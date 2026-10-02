"""
montauklabs: protect the images Lambda currently uses from the companion-repo lifecycle policy.

The companion-stack lifecycle policy keeps the newest N tagged images per repo. Lambda puts a
function in the Failed state if its image disappears from ECR, so an image that is still in use
must never be expired, even after a rollback or a pinned alias leaves it behind newer pushes.

Cleanup is paused before packaging/deploy. After a settled deploy, this reads every image
function's $LATEST and aliases (including weighted versions and nested stacks) from Lambda and
moves a `sam-in-use-*` tag onto each digest. Only complete discovery and tagging permits cleanup
to resume. A failed or cancelled operation leaves cleanup paused, preserving images even when
only some tags could be updated. Tags remain mutable so successful deploys can release old,
unaliased images to the existing count rule.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterator, List, Optional, Set, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from samcli.commands.exceptions import UserException
from samcli.lib.bootstrap.companion_stack.companion_stack_builder import (
    IN_USE_RULE_COUNT,
    IN_USE_TAG_PREFIX,
    companion_repo_retain_count,
)
from samcli.lib.bootstrap.companion_stack.data_types import CompanionStack
from samcli.lib.utils.hash import str_checksum

LOG = logging.getLogger(__name__)

MAX_WORKERS = 8
MAX_TAG_LENGTH = 128
MANIFEST_MEDIA_TYPES = [
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
]


class InUseImageProtectionError(UserException):
    def __init__(self, stack_name: str, failures: List[str]) -> None:
        details = "\n".join(f"\t- {failure}" for failure in failures)
        super().__init__(
            f"Stack {stack_name}: {len(failures)} in-use image(s) could not be protected from "
            f"the companion-repo lifecycle policy:\n{details}\n"
            "Fix the cause and redeploy, or set SAM_CLI_COMPANION_REPO_RETAIN_IMAGES=0 to disable retention."
        )


def in_use_tag(function_path: str, alias: Optional[str] = None) -> str:
    """
    Tag for one function reference. The function's logical path (stable across replacements) is
    hashed so functions that share a repo cannot take each other's tags.
    """
    qualifier = f"alias-{alias}" if alias else "latest"
    return f"{IN_USE_TAG_PREFIX}{str_checksum(function_path)[:8]}-{qualifier}"[:MAX_TAG_LENGTH]


def parse_resolved_image_uri(uri: Optional[str]) -> Optional[Tuple[str, str]]:
    """`<registry>/<repo>@sha256:...` -> (repo, digest); None if it is not a digest reference."""
    if not uri or "/" not in uri or "@" not in uri:
        return None
    repository, digest = uri.split("/", 1)[1].split("@", 1)
    return repository, digest


def _stack_resources(cfn, stack_name: str) -> Iterator[dict]:
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name):
        yield from page["StackResourceSummaries"]


def _companion_repos(cfn, stack_name: str) -> Optional[Set[str]]:
    """Repo names in the stack's companion stack; None when there is no companion stack."""
    companion_stack_name = CompanionStack(stack_name).stack_name
    try:
        return {
            resource["PhysicalResourceId"]
            for resource in _stack_resources(cfn, companion_stack_name)
            if resource["ResourceType"] == "AWS::ECR::Repository" and resource.get("PhysicalResourceId")
        }
    except ClientError as ex:
        if "does not exist" in ex.response.get("Error", {}).get("Message", ""):
            return None
        raise


def _stack_functions(cfn, stack_name: str, path: str = "") -> List[Tuple[str, str]]:
    """(logical path, function name) for every Lambda function in the stack and its nested stacks."""
    functions = []
    for resource in _stack_resources(cfn, stack_name):
        physical_id = resource.get("PhysicalResourceId")
        if not physical_id or resource["ResourceStatus"] == "DELETE_COMPLETE":
            continue
        logical_path = f"{path}/{resource['LogicalResourceId']}" if path else resource["LogicalResourceId"]
        if resource["ResourceType"] == "AWS::Lambda::Function":
            functions.append((logical_path, physical_id))
        elif resource["ResourceType"] == "AWS::CloudFormation::Stack":
            functions.extend(_stack_functions(cfn, physical_id, logical_path))
    return functions


def _function_references(
    lambda_client, function_path: str, function_name: str
) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """
    (repo, digest, tag) for the function's $LATEST and each alias, plus failures. Zip functions
    reference nothing.
    """
    references: List[Tuple[str, str, str]] = []
    failures: List[str] = []
    try:
        latest = lambda_client.get_function(FunctionName=function_name)
        if latest["Configuration"].get("PackageType") != "Image":
            return references, failures
        qualifiers: List[Tuple[Optional[str], dict, str]] = [(None, latest, in_use_tag(function_path))]
        for page in lambda_client.get_paginator("list_aliases").paginate(FunctionName=function_name):
            for alias in page["Aliases"]:
                version = lambda_client.get_function(FunctionName=function_name, Qualifier=alias["FunctionVersion"])
                qualifiers.append((alias["Name"], version, in_use_tag(function_path, alias["Name"])))
                for weighted_version in alias.get("RoutingConfig", {}).get("AdditionalVersionWeights", {}):
                    version = lambda_client.get_function(FunctionName=function_name, Qualifier=weighted_version)
                    qualifiers.append(
                        (
                            f"{alias['Name']}-version-{weighted_version}",
                            version,
                            in_use_tag(f"{function_path}/weighted", alias["Name"]),
                        )
                    )
    except ClientError as ex:
        return references, [f"{function_name}: {ex}"]

    for alias_name, response, tag in qualifiers:
        label = f"{function_name}:{alias_name}" if alias_name else function_name
        parsed = parse_resolved_image_uri(response.get("Code", {}).get("ResolvedImageUri"))
        if parsed is None:
            failures.append(f"{label}: no resolved image digest")
            continue
        references.append((parsed[0], parsed[1], tag))
    return references, failures


def _tag_image(ecr_client, repository: str, digest: str, tag: str) -> Optional[str]:
    """Point `tag` at `digest`; returns a failure message or None."""
    try:
        images = ecr_client.batch_get_image(
            repositoryName=repository,
            imageIds=[{"imageDigest": digest}],
            acceptedMediaTypes=MANIFEST_MEDIA_TYPES,
        )["images"]
        if not images:
            return f"{repository}@{digest}: image not found"
        image = images[0]
        put_args = {
            "repositoryName": repository,
            "imageManifest": image["imageManifest"],
            "imageTag": tag,
            "imageDigest": digest,
        }
        if image.get("imageManifestMediaType"):
            put_args["imageManifestMediaType"] = image["imageManifestMediaType"]
        ecr_client.put_image(**put_args)
    except ClientError as ex:
        if ex.response.get("Error", {}).get("Code") == "ImageAlreadyExistsException":
            return None  # the tag already points at this digest
        return f"{repository}@{digest} ({tag}): {ex}"
    return None


def protect_in_use_images(stack_name: str, region: Optional[str], boto_config: Optional[Config] = None) -> int:
    """
    Tag every image the stack's Lambda functions and aliases use so the companion-repo lifecycle
    policy keeps it. Only images in this stack's companion repos are tagged, since only those repos
    carry the policy. Returns the number of references protected.

    Raises InUseImageProtectionError if any reference could not be resolved or tagged.
    """
    if companion_repo_retain_count() is None:
        return 0
    cfn = boto3.client("cloudformation", region_name=region, config=boto_config)
    companion_repos = _companion_repos(cfn, stack_name)
    if not companion_repos:
        return 0

    lambda_client = boto3.client("lambda", region_name=region, config=boto_config)
    ecr_client = boto3.client("ecr", region_name=region, config=boto_config)
    functions = _stack_functions(cfn, stack_name)

    failures: List[str] = []
    wanted: Dict[Tuple[str, str], str] = {}  # (repo, tag) -> digest
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for references, function_failures in executor.map(
            lambda function: _function_references(lambda_client, *function), functions
        ):
            failures.extend(function_failures)
            for repository, digest, tag in references:
                if repository in companion_repos:
                    key = (repository, tag)
                    if key in wanted and wanted[key] != digest:
                        failures.append(f"{repository}: protection tag {tag} collides across live digests")
                    else:
                        wanted[key] = digest

        # Do not move any existing protection markers when discovery is incomplete.
        if failures:
            raise InUseImageProtectionError(stack_name, sorted(failures))
        # The priority-one rule is a finite count rule too. Include stale protection markers,
        # not just today's references, so resuming it cannot expire a live protected image.
        for repository in companion_repos:
            digests = {digest for (repo, _), digest in wanted.items() if repo == repository}
            for page in ecr_client.get_paginator("describe_images").paginate(
                repositoryName=repository, filter={"tagStatus": "TAGGED"}
            ):
                digests.update(
                    image["imageDigest"]
                    for image in page["imageDetails"]
                    if any(tag.startswith(IN_USE_TAG_PREFIX) for tag in image.get("imageTags", []))
                )
            if len(digests) > IN_USE_RULE_COUNT:
                failures.append(
                    f"{repository}: {len(digests)} protected images exceed the {IN_USE_RULE_COUNT} image limit"
                )
        if failures:
            raise InUseImageProtectionError(stack_name, sorted(failures))
        for failure in executor.map(
            lambda item: _tag_image(ecr_client, item[0][0], item[1], item[0][1]), wanted.items()
        ):
            if failure:
                failures.append(failure)

    if failures:
        raise InUseImageProtectionError(stack_name, sorted(failures))
    LOG.debug("Protected %d in-use image reference(s) for %s", len(wanted), stack_name)
    return len(wanted)

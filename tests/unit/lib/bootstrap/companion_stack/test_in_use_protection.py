from unittest import TestCase
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from samcli.lib.bootstrap.companion_stack.data_types import CompanionStack
from samcli.lib.bootstrap.companion_stack.in_use_protection import (
    InUseImageProtectionError,
    in_use_tag,
    parse_resolved_image_uri,
    protect_in_use_images,
)

MODULE = "samcli.lib.bootstrap.companion_stack.in_use_protection"
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
COMPANION = CompanionStack("app").stack_name


def _client_error(code, message="boom"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


def _resource(logical_id, resource_type, physical_id, status="UPDATE_COMPLETE"):
    return {
        "LogicalResourceId": logical_id,
        "ResourceType": resource_type,
        "PhysicalResourceId": physical_id,
        "ResourceStatus": status,
    }


def _paginator(pages_by_key, key_arg):
    paginator = Mock()
    paginator.paginate.side_effect = lambda **kwargs: pages_by_key[kwargs[key_arg]]
    return paginator


class FakeAws:
    """Mock CloudFormation, Lambda and ECR clients driven by plain dicts."""

    def __init__(self, stacks, functions, aliases=None, missing_stacks=()):
        self.stacks = stacks  # stack name -> resources
        self.functions = functions  # (function, qualifier or None) -> get_function response
        self.aliases = aliases or {}  # function -> [alias dicts]
        self.missing_stacks = set(missing_stacks)
        self.put_calls = []
        self.put_errors = {}  # tag -> ClientError

        self.cfn = Mock()
        self.cfn.get_paginator.return_value.paginate.side_effect = self._list_stack_resources
        self.lambda_client = Mock()
        self.lambda_client.get_function.side_effect = self._get_function
        self.lambda_client.get_paginator.return_value.paginate.side_effect = lambda FunctionName: [
            {"Aliases": self.aliases.get(FunctionName, [])}
        ]
        self.ecr = Mock()
        self.ecr.batch_get_image.side_effect = lambda **kwargs: {
            "images": [{"imageManifest": "{}", "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"}]
        }
        self.ecr.put_image.side_effect = self._put_image

    def _list_stack_resources(self, StackName):
        if StackName in self.missing_stacks:
            raise _client_error("ValidationError", f"Stack with id {StackName} does not exist")
        return [{"StackResourceSummaries": self.stacks[StackName]}]

    def _get_function(self, FunctionName, Qualifier=None):
        response = self.functions.get((FunctionName, Qualifier))
        if response is None:
            raise _client_error("ResourceNotFoundException")
        return response

    def _put_image(self, **kwargs):
        self.put_calls.append(kwargs)
        if kwargs["imageTag"] in self.put_errors:
            raise self.put_errors[kwargs["imageTag"]]

    def client(self, service, **_kwargs):
        return {"cloudformation": self.cfn, "lambda": self.lambda_client, "ecr": self.ecr}[service]

    def tags(self):
        return {(call["repositoryName"], call["imageTag"]): call["imageDigest"] for call in self.put_calls}


def _image(repo, digest):
    return {"Configuration": {"PackageType": "Image"}, "Code": {"ResolvedImageUri": f"{REGISTRY}/{repo}@{digest}"}}


class TestInUseProtection(TestCase):
    def _run(self, aws, env=None):
        with patch(f"{MODULE}.boto3.client", side_effect=aws.client), patch.dict("os.environ", env or {}, clear=True):
            return protect_in_use_images("app", "us-east-1")

    def _companion(self, *repos):
        return [_resource(f"Repo{n}", "AWS::ECR::Repository", repo) for n, repo in enumerate(repos)]

    def test_tags_latest_and_alias_digests_including_nested_stacks(self):
        aws = FakeAws(
            stacks={
                COMPANION: self._companion("app/fnarepo", "app/fnbrepo"),
                "app": [
                    _resource("FnA", "AWS::Lambda::Function", "app-FnA"),
                    _resource("Child", "AWS::CloudFormation::Stack", "arn:child"),
                ],
                "arn:child": [_resource("FnB", "AWS::Lambda::Function", "app-Child-FnB")],
            },
            functions={
                ("app-FnA", None): _image("app/fnarepo", "sha256:new"),
                ("app-FnA", "7"): _image("app/fnarepo", "sha256:old"),
                ("app-Child-FnB", None): _image("app/fnbrepo", "sha256:b"),
            },
            aliases={"app-FnA": [{"Name": "live", "FunctionVersion": "7"}]},
        )

        self.assertEqual(3, self._run(aws))
        self.assertEqual(
            {
                ("app/fnarepo", in_use_tag("FnA")): "sha256:new",
                ("app/fnarepo", in_use_tag("FnA", "live")): "sha256:old",
                ("app/fnbrepo", in_use_tag("Child/FnB")): "sha256:b",
            },
            aws.tags(),
        )
        self.assertTrue(all(call["imageManifestMediaType"] for call in aws.put_calls))

    def test_skips_zip_functions_and_repos_outside_the_companion_stack(self):
        aws = FakeAws(
            stacks={
                COMPANION: self._companion("app/fnarepo"),
                "app": [
                    _resource("Zip", "AWS::Lambda::Function", "app-Zip"),
                    _resource("Other", "AWS::Lambda::Function", "app-Other"),
                    _resource("Gone", "AWS::Lambda::Function", "app-Gone", status="DELETE_COMPLETE"),
                ],
            },
            functions={
                ("app-Zip", None): {"Configuration": {"PackageType": "Zip"}, "Code": {}},
                ("app-Other", None): _image("user-managed-repo", "sha256:x"),
            },
        )

        self.assertEqual(0, self._run(aws))
        self.assertEqual([], aws.put_calls)

    def test_tag_already_on_digest_is_success(self):
        aws = FakeAws(
            stacks={COMPANION: self._companion("r"), "app": [_resource("Fn", "AWS::Lambda::Function", "app-Fn")]},
            functions={("app-Fn", None): _image("r", "sha256:a")},
        )
        aws.put_errors[in_use_tag("Fn")] = _client_error("ImageAlreadyExistsException")

        self.assertEqual(1, self._run(aws))

    def test_fails_closed_but_still_tags_what_resolved(self):
        aws = FakeAws(
            stacks={
                COMPANION: self._companion("r1", "r2"),
                "app": [
                    _resource("Ok", "AWS::Lambda::Function", "app-Ok"),
                    _resource("NoDigest", "AWS::Lambda::Function", "app-NoDigest"),
                    _resource("Unreadable", "AWS::Lambda::Function", "app-Unreadable"),
                    _resource("TagFails", "AWS::Lambda::Function", "app-TagFails"),
                ],
            },
            functions={
                ("app-Ok", None): _image("r1", "sha256:ok"),
                ("app-NoDigest", None): {"Configuration": {"PackageType": "Image"}, "Code": {"ImageUri": "r:tag"}},
                ("app-TagFails", None): _image("r2", "sha256:t"),
            },
        )
        aws.put_errors[in_use_tag("TagFails")] = _client_error("ImageTagAlreadyExistsException")

        with self.assertRaises(InUseImageProtectionError) as ctx:
            self._run(aws)

        message = str(ctx.exception)
        self.assertIn("3 in-use image(s)", message)
        self.assertIn("app-NoDigest: no resolved image digest", message)
        self.assertIn("app-Unreadable", message)
        self.assertIn("ImageTagAlreadyExistsException", message)
        self.assertIn(("r1", in_use_tag("Ok")), aws.tags())

    def test_no_companion_stack_is_a_no_op(self):
        aws = FakeAws(stacks={}, functions={}, missing_stacks=[COMPANION])

        self.assertEqual(0, self._run(aws))
        aws.lambda_client.get_function.assert_not_called()

    def test_disabled_retention_is_a_no_op(self):
        aws = FakeAws(stacks={}, functions={})

        self.assertEqual(0, self._run(aws, {"SAM_CLI_COMPANION_REPO_RETAIN_IMAGES": "0"}))
        aws.cfn.get_paginator.assert_not_called()

    def test_other_companion_stack_errors_propagate(self):
        aws = FakeAws(stacks={}, functions={})
        aws.cfn.get_paginator.return_value.paginate.side_effect = _client_error("AccessDenied", "denied")

        with self.assertRaises(ClientError):
            self._run(aws)


class TestHelpers(TestCase):
    def test_in_use_tag_is_stable_per_function_and_qualifier(self):
        self.assertEqual(in_use_tag("Child/Fn"), in_use_tag("Child/Fn"))
        self.assertNotEqual(in_use_tag("Child/Fn"), in_use_tag("Other/Fn"))
        self.assertTrue(in_use_tag("Fn").startswith("sam-in-use-"))
        self.assertTrue(in_use_tag("Fn").endswith("-latest"))
        self.assertTrue(in_use_tag("Fn", "live").endswith("-alias-live"))
        self.assertEqual(128, len(in_use_tag("Fn", "a" * 200)))

    def test_parse_resolved_image_uri(self):
        self.assertEqual(("ns/repo", "sha256:abc"), parse_resolved_image_uri(f"{REGISTRY}/ns/repo@sha256:abc"))
        self.assertIsNone(parse_resolved_image_uri(f"{REGISTRY}/ns/repo:tag"))
        self.assertIsNone(parse_resolved_image_uri(None))

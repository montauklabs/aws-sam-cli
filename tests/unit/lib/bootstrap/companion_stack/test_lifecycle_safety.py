"""Exercise real CLI/companion/protection ordering without making AWS requests."""

import copy
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, WaiterError
from parameterized import parameterized

from samcli.commands.deploy.command import do_cli
from samcli.commands.deploy.exceptions import ChangeEmptyError, DeployFailedError
from samcli.lib.bootstrap.companion_stack import companion_stack_manager as manager_module
from samcli.lib.bootstrap.companion_stack.companion_stack_builder import companion_repo_lifecycle_policy
from samcli.lib.bootstrap.companion_stack.data_types import CompanionStack, ECRRepo
from samcli.lib.bootstrap.companion_stack.in_use_protection import InUseImageProtectionError, in_use_tag
from samcli.lib.deploy.deployer import Deployer
from tests.unit.lib.bootstrap.companion_stack.test_in_use_protection import FakeAws, _client_error, _image, _resource

STACK = "app"
COMPANION = CompanionStack(STACK)
REPO = ECRRepo(COMPANION, "Fn").physical_id


class LifecycleAws(FakeAws):
    def __init__(self, active=False, new_companion=False):
        super().__init__(
            stacks={
                COMPANION.stack_name: [_resource("Repo", "AWS::ECR::Repository", REPO)],
                STACK: [_resource("Fn", "AWS::Lambda::Function", "app-Fn")],
            },
            functions={("app-Fn", None): _image(REPO, "sha256:0")},
        )
        self.events = []
        self.live_digests = {"sha256:0"}
        self.tags_by_digest = {f"sha256:{n}": {f"release-{n}"} for n in range(101)}
        self.template = {
            "Resources": {"Repo": {"Type": "AWS::ECR::Repository", "Properties": {"RepositoryName": REPO}}}
        }
        self.exists = not new_companion
        if active:
            self.template["Resources"]["Repo"]["Properties"]["LifecyclePolicy"] = {
                "LifecyclePolicyText": companion_repo_lifecycle_policy()
            }
        self.cfn.meta.region_name = "us-east-1"
        self.cfn.get_template.side_effect = self._get_template
        self.cfn.describe_stacks.side_effect = self._describe_stacks
        self.cfn.update_stack.side_effect = self._update_stack
        self.cfn.create_stack.side_effect = self._update_stack
        self.s3 = Mock()
        self.s3._client_config.region_name = "us-east-1"
        self.sts = Mock()
        self.sts.get_caller_identity.return_value = {"Account": "123456789012"}
        self.ecr.get_paginator.return_value.paginate.side_effect = lambda **kwargs: [
            {
                "imageDetails": [
                    {"imageDigest": digest, "imageTags": list(tags)} for digest, tags in self.tags_by_digest.items()
                ]
            }
        ]

    def _get_template(self, **kwargs):
        if not self.exists:
            raise _client_error("ValidationError", f"Stack with id {COMPANION.stack_name} does not exist")
        return {"TemplateBody": copy.deepcopy(self.template)}

    def _describe_stacks(self, **kwargs):
        if not self.exists:
            raise _client_error("ValidationError", f"Stack with id {COMPANION.stack_name} does not exist")
        return {"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}

    def _update_stack(self, **kwargs):
        self.template = json.loads(kwargs["TemplateBody"]) if "TemplateBody" in kwargs else self.uploaded_template
        self.exists = True
        self.events.append("cleanup-enabled" if self.policy() else "cleanup-paused")
        # Evaluate immediately, at the worst possible time, rather than assume an ECR grace period.
        if self.policy():
            assert not self.live_digests & self.eligible(), "live digest was exposed at policy installation"
        return {}

    def policy(self):
        return next(
            (resource["Properties"].get("LifecyclePolicy") for resource in self.template["Resources"].values()), None
        )

    def eligible(self):
        if not self.policy():
            return set()
        rules = json.loads(self.policy()["LifecyclePolicyText"])["rules"]
        protected = {
            digest for digest, tags in self.tags_by_digest.items() if any(tag.startswith("sam-in-use-") for tag in tags)
        }
        # Inventory insertion order represents pushed_at_time from oldest to newest.
        all_tagged = [digest for digest, tags in self.tags_by_digest.items() if tags]
        count = rules[1]["selection"]["countNumber"]
        return set(all_tagged[:-count]) - protected

    def _put_image(self, **kwargs):
        super()._put_image(**kwargs)
        tag = kwargs["imageTag"]
        for tags in self.tags_by_digest.values():
            tags.discard(tag)
        self.tags_by_digest[kwargs["imageDigest"]].add(tag)
        self.events.append("tagged")

    def upload(self, path, *_args):
        self.uploaded_template = json.loads(Path(path).read_text())
        return "s3://bucket/template"

    def client(self, service, **kwargs):
        if service in ("s3", "sts"):
            return {"s3": self.s3, "sts": self.sts}[service]
        return super().client(service, **kwargs)


class TestLifecycleSafety(TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.aws = LifecycleAws()
        self.failure = None
        self.package_failure = None
        self.tempdir = self.contexts.enter_context(tempfile.TemporaryDirectory())
        self.template = Path(self.tempdir) / "template.json"
        self.template.write_text(
            json.dumps(
                {
                    "Resources": {
                        "Fn": {
                            "Type": "AWS::Serverless::Function",
                            "Properties": {"PackageType": "Image", "ImageUri": "local:latest"},
                        }
                    }
                }
            )
        )
        self.contexts.enter_context(
            patch("boto3.client", side_effect=lambda service, **kwargs: self.aws.client(service, **kwargs))
        )
        uploader = self.contexts.enter_context(patch(f"{manager_module.__name__}.S3Uploader"))
        uploader.return_value.upload_with_dedup.side_effect = lambda *args: self.aws.upload(*args)
        uploader.return_value.to_path_style_s3_url.return_value = "https://s3.amazonaws.com/bucket/template"
        resource = self.contexts.enter_context(patch("boto3.resource"))
        summary = Mock()
        summary.resource_type = "AWS::ECR::Repository"
        summary.logical_resource_id = ECRRepo(COMPANION, "Fn").logical_id
        summary.physical_resource_id = REPO
        resource.return_value.Stack.return_value.resource_summaries.all.return_value = [summary]
        self.package = self.contexts.enter_context(patch("samcli.commands.package.package_context.PackageContext"))
        self.package.side_effect = self._package
        self.contexts.enter_context(
            patch.object(Deployer, "create_and_wait_for_changeset", return_value=({"Id": "changeset"}, "UPDATE"))
        )
        self.contexts.enter_context(patch.object(Deployer, "get_last_event_time", return_value=0))
        self.contexts.enter_context(patch.object(Deployer, "execute_changeset", side_effect=self._execute))
        self.contexts.enter_context(patch.object(Deployer, "wait_for_execute", side_effect=self._wait))
        self.contexts.enter_context(patch.object(Deployer, "sync", side_effect=self._wait))
        self.contexts.enter_context(patch.dict("os.environ", {"SAM_CLI_COMPANION_REPO_RETAIN_IMAGES": "100"}))

    def _package(self, **kwargs):
        context = Mock()

        def run():
            self.aws.events.append("packaged")
            self.assertIsNone(self.aws.policy())
            if self.package_failure:
                raise self.package_failure
            Path(kwargs["output_template_file"]).write_text("{}")

        context.run.side_effect = run
        context.__enter__ = Mock(return_value=context)
        context.__exit__ = Mock(return_value=False)
        return context

    def _execute(self, *_args, **_kwargs):
        self.aws.events.append("executed")
        self.assertIsNone(self.aws.policy())

    def _wait(self, *_args, **_kwargs):
        if self.failure:
            raise self.failure
        return {}

    def _run(self, **overrides):
        args = dict(
            template_file=str(self.template),
            stack_name=STACK,
            s3_bucket="bucket",
            image_repository=None,
            image_repositories={},
            force_upload=False,
            parallel_upload=False,
            no_progressbar=True,
            s3_prefix="app",
            kms_key_id=None,
            parameter_overrides={},
            capabilities=[],
            no_execute_changeset=False,
            role_arn=None,
            notification_arns=[],
            fail_on_empty_changeset=False,
            use_json=False,
            tags={},
            metadata=None,
            guided=False,
            confirm_changeset=False,
            region="us-east-1",
            profile=None,
            signing_profiles=None,
            resolve_s3=False,
            config_file=None,
            config_env="default",
            resolve_image_repos=True,
            language_extensions=False,
            disable_rollback=False,
            on_failure=None,
            max_wait_duration=60,
            express=False,
        )
        args.update(overrides)
        do_cli(**args)

    def test_first_install_oldest_of_101_is_protected_before_cleanup_activates(self):
        self._run()
        self.assertEqual(["cleanup-paused", "packaged", "executed", "tagged", "cleanup-enabled"], self.aws.events)
        self.assertNotIn("sha256:0", self.aws.eligible())

    @parameterized.expand([(False,), (True,)])
    def test_failed_deployment_preserves_oldest_live_image(self, disable_rollback):
        self.failure = DeployFailedError(STACK, "deployment failed")
        with self.assertRaises(DeployFailedError):
            self._run(disable_rollback=disable_rollback)
        self.assertIsNone(self.aws.policy())
        self.assertNotIn("sha256:0", self.aws.eligible())
        self.assertEqual([], self.aws.put_calls)

    def test_existing_policy_is_paused_before_packaging(self):
        self.aws = LifecycleAws(active=True)
        self.aws.tags_by_digest["sha256:0"].add(in_use_tag("Fn"))
        self.package_failure = RuntimeError("package failed")
        with self.assertRaisesRegex(RuntimeError, "package failed"):
            self._run()
        self.assertIsNone(self.aws.policy())
        self.assertIn(in_use_tag("Fn"), self.aws.tags_by_digest["sha256:0"])
        self.assertEqual("cleanup-paused", self.aws.events[0])

    @parameterized.expand([(RuntimeError("packaging failed"),), (KeyboardInterrupt(),)])
    def test_failure_or_cancellation_during_packaging_leaves_cleanup_paused(self, failure):
        self.package_failure = failure
        with self.assertRaises(type(failure)):
            self._run()
        self.assertIsNone(self.aws.policy())
        self.assertNotIn("executed", self.aws.events)

    @parameterized.expand([("discovery",), ("tagging",)])
    def test_protection_failure_never_restores_cleanup(self, stage):
        if stage == "discovery":
            self.aws.functions = {}
        else:
            self.aws.put_errors[in_use_tag("Fn")] = _client_error("AccessDeniedException")
        with self.assertRaises(InUseImageProtectionError):
            self._run()
        self.assertIsNone(self.aws.policy())
        self.assertNotIn("cleanup-enabled", self.aws.events)
        self.assertNotIn("sha256:0", self.aws.eligible())

    def test_pause_failure_aborts_before_packaging_or_tagging(self):
        self.aws = LifecycleAws(active=True)
        self.aws.cfn.update_stack.side_effect = _client_error("AccessDenied")
        with self.assertRaises(ClientError):
            self._run()
        self.package.assert_not_called()
        self.assertEqual([], self.aws.put_calls)

    @parameterized.expand([("express",), ("no_execute_changeset",)])
    def test_unsettled_or_unexecuted_deployment_leaves_cleanup_paused(self, option):
        self._run(**{option: True})
        self.assertIsNone(self.aws.policy())
        self.assertNotIn("cleanup-enabled", self.aws.events)

    def test_new_companion_starts_without_cleanup(self):
        self.aws = LifecycleAws(new_companion=True)
        self._run()
        self.assertEqual("cleanup-paused", self.aws.events[0])
        self.assertEqual("cleanup-enabled", self.aws.events[-1])

    def test_resume_failure_uses_the_paused_template_as_rollback_baseline(self):
        original_update = self.aws._update_stack

        def update(**kwargs):
            before = copy.deepcopy(self.aws.template)
            original_update(**kwargs)
            if self.aws.policy():
                self.assertNotIn("LifecyclePolicy", next(iter(before["Resources"].values()))["Properties"])
                self.aws.template = before  # model CloudFormation rollback to previous desired state
                raise _client_error("ValidationError", "restore failed")

        self.aws.cfn.update_stack.side_effect = update
        with self.assertRaises(ClientError):
            self._run()
        self.assertIsNone(self.aws.policy())

    def test_pause_waiter_timeout_aborts_before_packaging(self):
        self.aws = LifecycleAws(active=True)
        self.aws.tags_by_digest["sha256:0"].add(in_use_tag("Fn"))
        self.aws.cfn.get_waiter.return_value.wait.side_effect = WaiterError(
            name="StackUpdateComplete", reason="max attempts", last_response={}
        )
        with self.assertRaises(WaiterError):
            self._run()
        self.package.assert_not_called()
        self.assertEqual([], self.aws.put_calls)
        self.assertNotIn("cleanup-enabled", self.aws.events)

    def test_retry_waits_for_prior_pause_before_packaging(self):
        self.aws.cfn.describe_stacks.side_effect = None
        self.aws.cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]}
        waiter = self.aws.cfn.get_waiter.return_value

        def settled(**kwargs):
            self.aws.cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}
            self.aws.events.append("settled")

        waiter.wait.side_effect = settled
        self._run()
        self.assertLess(self.aws.events.index("settled"), self.aws.events.index("packaged"))

    def test_successful_no_change_deployment_restores_cleanup(self):
        with patch.object(Deployer, "create_and_wait_for_changeset", side_effect=ChangeEmptyError(STACK)):
            self._run()
        self.assertEqual("cleanup-enabled", self.aws.events[-1])
        self.assertNotIn("executed", self.aws.events)
        self.assertNotIn("sha256:0", self.aws.eligible())

    def test_successful_latest_move_releases_old_unaliased_image(self):
        self.aws = LifecycleAws(active=True)
        self.aws.tags_by_digest["sha256:0"].add(in_use_tag("Fn"))

        def deployed(*args, **kwargs):
            self.aws.functions[("app-Fn", None)] = _image(REPO, "sha256:100")
            self.aws.live_digests = {"sha256:100"}

        with patch.object(Deployer, "wait_for_execute", side_effect=deployed):
            self._run()
        self.assertIn("sha256:0", self.aws.eligible())
        self.assertNotIn("sha256:100", self.aws.eligible())
        self.assertNotIn(in_use_tag("Fn"), self.aws.tags_by_digest["sha256:0"])
        self.assertIn(in_use_tag("Fn"), self.aws.tags_by_digest["sha256:100"])

    def test_template_parameters_and_other_resources_are_preserved(self):
        self.aws.tags_by_digest["sha256:0"].add(in_use_tag("Fn"))
        self.aws.template["Parameters"] = {"Name": {"Type": "String", "Default": "default"}}
        self.aws.template["Resources"]["Other"] = {
            "Type": "AWS::S3::Bucket",
            "Properties": {"BucketName": {"Ref": "Name"}},
        }
        before = copy.deepcopy(self.aws.template)
        manager_module.set_ecr_stack_lifecycle_policy(STACK, "us-east-1", None, None, enabled=True)
        args = self.aws.cfn.update_stack.call_args.kwargs
        self.assertEqual([{"ParameterKey": "Name", "UsePreviousValue": True}], args["Parameters"])
        self.assertEqual(before["Resources"]["Other"], self.aws.template["Resources"]["Other"])
        self.assertEqual(before["Parameters"], self.aws.template["Parameters"])

    def test_large_template_upload_honors_configured_kms_key(self):
        self.aws.tags_by_digest["sha256:0"].add(in_use_tag("Fn"))
        self.aws.template["Metadata"] = {"padding": "x" * 52000}
        uploader = self.contexts.enter_context(patch(f"{manager_module.__name__}.S3Uploader"))
        uploader.return_value.upload_with_dedup.side_effect = self.aws.upload
        uploader.return_value.to_path_style_s3_url.return_value = "https://s3.amazonaws.com/bucket/template"
        manager_module.set_ecr_stack_lifecycle_policy(
            STACK, "us-east-1", "bucket", "prefix", enabled=True, kms_key_id="configured-key"
        )
        self.assertEqual("configured-key", uploader.call_args.kwargs["kms_key_id"])
        self.assertIn("TemplateURL", self.aws.cfn.update_stack.call_args.kwargs)

    def test_small_template_uses_body_without_an_artifact_upload(self):
        self.aws.tags_by_digest["sha256:0"].add(in_use_tag("Fn"))
        with patch(f"{manager_module.__name__}.S3Uploader") as uploader:
            manager_module.set_ecr_stack_lifecycle_policy(STACK, "us-east-1", "bucket", "prefix", enabled=True)
        uploader.assert_not_called()
        self.assertIn("TemplateBody", self.aws.cfn.update_stack.call_args.kwargs)

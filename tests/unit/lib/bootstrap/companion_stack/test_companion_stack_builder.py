import json

from parameterized import parameterized

from samcli.lib.bootstrap.companion_stack.companion_stack_builder import (
    CompanionStackBuilder,
    companion_repo_lifecycle_policy,
)
from unittest import TestCase
from unittest.mock import Mock, patch


class TestCompanionStackBuilder(TestCase):
    @patch("samcli.lib.bootstrap.companion_stack.companion_stack_builder.ECRRepo")
    def test_building_single_function(self, ecr_repo_mock):
        companion_stack_name = "CompanionStackA"
        function_a = "FunctionA"

        repo_logical_id = "RepoLogicalIDA"
        repo_physical_id = "RepoPhysicalIDA"
        repo_output_id = "RepoOutputIDA"

        ecr_repo_instance = ecr_repo_mock.return_value
        ecr_repo_instance.logical_id = repo_logical_id
        ecr_repo_instance.physical_id = repo_physical_id
        ecr_repo_instance.output_logical_id = repo_output_id

        companion_stack = Mock()
        companion_stack.stack_name = companion_stack_name
        builder = CompanionStackBuilder(companion_stack)

        builder.add_function(function_a)
        template = builder.build()
        self.assertIn(f'"{repo_logical_id}":', template)
        self.assertIn(f'"RepositoryName": "{repo_physical_id}"', template)
        self.assertIn(f'"{repo_output_id}":', template)

    @patch("samcli.lib.bootstrap.companion_stack.companion_stack_builder.ECRRepo")
    def test_building_multiple_functions(self, ecr_repo_mock):
        companion_stack_name = "CompanionStackA"
        function_prefix = "Function"
        function_names = ["A", "B", "C", "D", "E", "F"]

        repo_logical_id_prefix = "RepoLogicalID"
        repo_physical_id_prefix = "RepoPhysicalID"
        repo_output_id_prefix = "RepoOutputID"

        ecr_repo_instances = list()
        for function_name in function_names:
            ecr_repo_instance = Mock()
            ecr_repo_instance.logical_id = repo_logical_id_prefix + function_name
            ecr_repo_instance.physical_id = repo_physical_id_prefix + function_name
            ecr_repo_instance.output_logical_id = repo_output_id_prefix + function_name
            ecr_repo_instances.append(ecr_repo_instance)

        ecr_repo_mock.side_effect = ecr_repo_instances

        companion_stack = Mock()
        companion_stack.stack_name = companion_stack_name
        builder = CompanionStackBuilder(companion_stack)

        for function_name in function_names:
            builder.add_function(function_prefix + function_name)
        template = builder.build()
        for function_name in function_names:
            self.assertIn(f'"{repo_logical_id_prefix + function_name}":', template)
            self.assertIn(f'"RepositoryName": "{repo_physical_id_prefix + function_name}"', template)
            self.assertIn(f'"{repo_output_id_prefix + function_name}":', template)

    @patch("samcli.lib.bootstrap.companion_stack.companion_stack_builder.ECRRepo")
    def test_mapping_multiple_functions(self, ecr_repo_mock):
        companion_stack_name = "CompanionStackA"
        function_prefix = "Function"
        function_names = ["A", "B", "C", "D", "E", "F"]

        repo_logical_id_prefix = "RepoLogicalID"
        repo_physical_id_prefix = "RepoPhysicalID"
        repo_output_id_prefix = "RepoOutputID"

        ecr_repo_instances = list()
        for function_name in function_names:
            ecr_repo_instance = Mock()
            ecr_repo_instance.logical_id = repo_logical_id_prefix + function_name
            ecr_repo_instance.physical_id = repo_physical_id_prefix + function_name
            ecr_repo_instance.output_logical_id = repo_output_id_prefix + function_name
            ecr_repo_instances.append(ecr_repo_instance)

        ecr_repo_mock.side_effect = ecr_repo_instances

        companion_stack = Mock()
        companion_stack.stack_name = companion_stack_name
        builder = CompanionStackBuilder(companion_stack)

        for function_name in function_names:
            builder.add_function(function_prefix + function_name)
        for function_name in function_names:
            self.assertIn(
                (function_prefix + function_name, ecr_repo_instances[function_names.index(function_name)]),
                builder.repo_mapping.items(),
            )

    def _build_single_repo(self):
        repo = Mock()
        repo.logical_id = "RepoLogicalIDA"
        repo.physical_id = "RepoPhysicalIDA"
        repo.output_logical_id = "RepoOutputIDA"
        companion_stack = Mock()
        companion_stack.stack_name = "CompanionStackA"
        with patch("samcli.lib.bootstrap.companion_stack.companion_stack_builder.ECRRepo", return_value=repo):
            builder = CompanionStackBuilder(companion_stack)
            builder.add_function("FunctionA")
            return builder._build_repo_dict(repo)

    def test_repos_get_a_keep_newest_images_lifecycle_policy_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            repo_dict = self._build_single_repo()

        policy = json.loads(repo_dict["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"])
        selection = policy["rules"][0]["selection"]
        self.assertEqual("tagged", selection["tagStatus"])
        self.assertEqual(["*"], selection["tagPatternList"])
        self.assertEqual("imageCountMoreThan", selection["countType"])
        self.assertEqual(100, selection["countNumber"])
        self.assertEqual({"type": "expire"}, policy["rules"][0]["action"])

    @parameterized.expand([("25", 25), ("abc", 100), ("", 100)])
    def test_lifecycle_policy_count_from_environment(self, value, expected):
        with patch.dict("os.environ", {"SAM_CLI_COMPANION_REPO_RETAIN_IMAGES": value}, clear=True):
            policy = json.loads(companion_repo_lifecycle_policy())

        self.assertEqual(expected, policy["rules"][0]["selection"]["countNumber"])

    @parameterized.expand([("0",), ("-1",)])
    def test_lifecycle_policy_can_be_disabled(self, value):
        with patch.dict("os.environ", {"SAM_CLI_COMPANION_REPO_RETAIN_IMAGES": value}, clear=True):
            repo_dict = self._build_single_repo()

        self.assertNotIn("LifecyclePolicy", repo_dict["Properties"])

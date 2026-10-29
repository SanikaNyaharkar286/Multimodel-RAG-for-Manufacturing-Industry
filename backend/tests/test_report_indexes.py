"""Index deployment validates offline and starts independent builds together."""
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('report_index_deployment',
    Path(__file__).resolve().parents[1] / 'scripts' / 'deploy_report_indexes.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class ReportIndexDeploymentTests(unittest.TestCase):
    def test_default_validation_never_connects_to_cloud(self):
        with patch('sys.argv', ['deploy', '--project', 'test-project', '--database', 'test-database']), \
             patch.object(deployment.firestore_admin_v1, 'FirestoreAdminClient') as client, redirect_stdout(io.StringIO()) as output:
            deployment.main()
        client.assert_not_called()
        self.assertIn('Validated 14 index definitions', output.getvalue())

    def test_all_indexes_are_requested_before_waiting_on_a_build(self):
        with patch('sys.argv', ['deploy', '--project', 'test-project', '--database', 'test-database', '--apply']), \
             patch.object(deployment.firestore_admin_v1, 'FirestoreAdminClient') as factory, redirect_stdout(io.StringIO()):
            client = factory.return_value.__enter__.return_value
            operation = Mock()
            client.create_index.return_value = operation
            operation.result.side_effect = lambda **kwargs: self.assertEqual(client.create_index.call_count, 14)
            deployment.main()
        self.assertEqual(operation.result.call_count, 14)

    def test_no_wait_submits_but_does_not_block_on_long_running_builds(self):
        with patch('sys.argv', ['deploy', '--project', 'test-project', '--database', 'test-database', '--apply', '--no-wait']), \
             patch.object(deployment.firestore_admin_v1, 'FirestoreAdminClient') as factory, redirect_stdout(io.StringIO()):
            deployment.main()
            client = factory.return_value.__enter__.return_value
        self.assertEqual(client.create_index.call_count, 14)
        client.create_index.return_value.result.assert_not_called()

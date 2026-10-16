import unittest

import test_p2d as p2d
from project_sessions import registry as registry_module


class SubmitErrorTests(unittest.TestCase):
    setUp = p2d.DelegationTests.setUp
    tearDown = p2d.DelegationTests.tearDown
    register = p2d.DelegationTests.register
    tool = p2d.DelegationTests.tool
    hook = p2d.DelegationTests.hook
    args = p2d.DelegationTests.args

    def test_invented_workspace_gets_actionable_error_without_creation(self):
        # The main model once invented new_workstream.workspace for a fresh topic and got only the
        # generic error, costing two extra model rounds (tool_describe + project_status) to recover.
        missing = str(self.home / 'invented-dir')
        for args in (self.args(new_workstream={'scope': 'topic', 'workspace': missing}), self.args(workspace=missing)):
            error = self.tool('project_submit', args)['error']
            self.assertIn('existing directory', error)
            self.assertIn('omit', error)
            self.assertNotIn(missing, error)
        self.assertEqual(self.reg.inspect(self.pid, self.route)['workstreams'], [])
        self.assertEqual(self.tool('project_submit', self.args())['state'], 'queued')

    def test_reused_request_key_gets_actionable_error(self):
        # Changed tasks within one project must use a new key; /new gets its own namespace.
        first = self.tool('project_submit', self.args('tech-deals-20261014'))
        changed = self.args('tech-deals-20261014', task='A different task')
        error = self.tool('project_submit', changed)['error']
        self.assertIn('request_key', error)
        self.assertIn('new key', error)
        self.hook('new', [])
        other = self.tool('project_submit', self.args('tech-deals-20261014'), 'new')
        self.assertNotIn('error', other)
        self.assertNotEqual(other['id'], first['id'])
        self.assertEqual(len(self.reg.inspect(self.pid, self.route)['tasks']), 1)
        self.assertEqual(self.tool('project_submit', self.args('tech-deals-20261014'))['id'], first['id'])

    def test_submit_description_explains_workspace_defaults(self):
        prompt = self.module.PROMPT
        description = self.tools['project_submit']['schema']['description']
        self.assertIn('workspace', description)
        self.assertIn('private default', description)
        self.assertIn('must exist', description)
        self.assertLess(len(description), 400)
        self.assertEqual(self.module.orchestrator_section({'platform': 'telegram'}), prompt)


if __name__ == '__main__':
    unittest.main()

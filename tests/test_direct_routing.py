"""Routing intent regressions; no model, audio, or runtime writes."""
import unittest

from ramble.direct_routing import needs_router


class RoutingIntentTests(unittest.TestCase):
    def test_discussion_references_and_constraints_keep_current_owner(self):
        for text in (
            'Ramble, did you get my instructions about launching app agents?',
            'Ramble, can you respond before you do any work? I asked you to show '
            'the prompt before you launch the agents. Just show me the prompt first.',
            'What did the other agent find?',
            'Explain how sessions and threads work.',
            'Did you create the new session?',
            'Do not launch any agents yet.',
            'Before you start another session, show me the prompt.',
            'Tell me about the agents you launched.',
            'Can you explain what "stop working" means?',
            'Read this example: "First. Create a new session. Then stop."',
            "Read this example: 'First. Create a new session.'",
            'I asked you to switch to the other session earlier. Did that happen?',
            'Should we create a separate session?',
            'I am thinking about creating a reminder.',
        ):
            with self.subTest(text=text):
                self.assertFalse(needs_router(text))

    def test_explicit_destinations_delegation_and_adapters_use_router(self):
        for text in (
            'Ramble, switch to the presentation session.',
            'Actually, put this in a separate session.',
            'Ramble, please start a new conversation.',
            'Can you open the presentation thread?',
            'I want you to move this to another session.',
            "Let's create a separate session.",
            'Ramble, have an agent investigate this.',
            'Please launch the app agents.',
            'Ramble, could you ask a separate agent to research this?',
            'Delegate this investigation.',
            'First answer my question. Then create a new session.',
            'Ramble, remind me to call tomorrow.',
            'Can you please change my time entry?',
            'What am I tracking?',
            'Ramble, start a new Toggl timer for Example Engineering.',
            'Start a new timer under engineering.',
            'Start tracking debugging.',
            'Show my reminders.',
            'Stop talking.',
            'Publish the report in Companion.',
        ):
            with self.subTest(text=text):
                self.assertTrue(needs_router(text))

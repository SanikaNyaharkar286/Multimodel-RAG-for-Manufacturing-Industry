import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.chat_service import answer_question
from app.agent.inventory import is_inventory_request, inventory_response
from app.responses.schemas import QueryPlan


DOCUMENTS = [
    {'machine_name': 'HSG-2550', 'document_name': 'Operation'},
    {'machine_name': 'HSG-2550', 'document_name': 'Maintenance'},
    {'machine_name': 'HAB-3232', 'document_name': 'Operation'},
]


class InventoryTests(unittest.TestCase):
    def test_catalog_phrases_and_typing_variants(self):
        for question in (
            'how many machine documents u have', 'How many manuals do you have?',
            'how many machne documnts u have', 'which machine documents do you have',
            'what manuals', 'available machines', 'list all documents',
            'show your available manuals', 'how many machines are indexed',
        ):
            with self.subTest(question=question):
                self.assertTrue(is_inventory_request(question))

    def test_manual_content_questions_are_not_catalog_requests(self):
        for question in (
            'How many switches does the machine have?',
            'What do the machine documents say about pressure?',
            'How many pages are in the manual?',
            'Show the pressure gauge in the manual',
            'Which machine documents the fault history?',
        ):
            with self.subTest(question=question):
                self.assertFalse(is_inventory_request(question))

    def test_counts_distinct_machine_document_pairs(self):
        response = inventory_response(DOCUMENTS + [DOCUMENTS[0]])
        self.assertIn('**3 indexed documents**', response)
        self.assertIn('**2 machine models**', response)
        self.assertIn('HAB-3232', response)
        self.assertIn('Maintenance', response)
        self.assertIn('**0 indexed machine documents**', inventory_response([]))


class InventoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot_question_bypasses_models_even_after_photo(self):
        entry = SimpleNamespace(history=[], uploaded_photo=b'earlier-photo')
        with (
            patch('app.agent.chat_service.store.state', new=AsyncMock(return_value={'main_topic': 'pressure gauge'})),
            patch('app.agent.chat_service.list_available_documents', return_value=DOCUMENTS) as catalog,
            patch('app.agent.chat_service.understand') as planner,
            patch('app.agent.chat_service.search_manual') as search,
            patch('app.agent.chat_service.runner.run_async') as runner,
        ):
            response, used_runner = await answer_question(entry, 'how many machine documents u have', None)
        catalog.assert_called_once_with()
        planner.assert_not_called()
        search.assert_not_called()
        runner.assert_not_called()
        self.assertFalse(used_runner)
        self.assertIn('**3 indexed documents**', response['blocks'][0]['content'])

    async def test_planner_inventory_intent_uses_database_not_manual_search(self):
        selected = QueryPlan(intent='document_inventory', query='catalog', topic='',
            follow_up=False, guided=False, advance=False, wants_visual=False,
            wants_video=False, wants_sources=False, clarify='', out_of_scope=False,
            model_mismatch=False)
        with (
            patch('app.agent.chat_service.store.state', new=AsyncMock(return_value={})),
            patch('app.agent.chat_service.understand', return_value=selected),
            patch('app.agent.chat_service.list_available_documents', return_value=DOCUMENTS),
            patch('app.agent.chat_service.search_manual') as search,
        ):
            response, used_runner = await answer_question(SimpleNamespace(history=[]),
                'Tell me the size of your collection of equipment handbooks', None)
        search.assert_not_called()
        self.assertFalse(used_runner)
        self.assertIn('**3 indexed documents**', response['blocks'][0]['content'])

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.agent.conversation_context import latest_user_photo, recent_context
from app.agent.planner import understand, _provider_fallback_plan
from app.agent.chat_service import answer_question, _smalltalk_reply
from app.responses.schemas import QueryPlan


PHOTO = b'\x89PNG\r\n\x1a\nprevious-user-photo'
NEW_PHOTO = b'\x89PNG\r\n\x1a\nnew-user-photo'


def upload(data=PHOTO):
    return {'role': 'user', 'text': '', 'image':
            'data:image/png;base64,' + base64.b64encode(data).decode()}


def plan(**changes):
    values = dict(query='pressure gauge purpose and operation', topic='pressure gauge',
                  follow_up=True, use_previous_photo=True, guided=False, advance=False,
                  wants_visual=False, wants_video=False, wants_sources=False,
                  clarify='', out_of_scope=False, model_mismatch=False)
    return QueryPlan(**(values | changes))


class ContextTests(unittest.TestCase):
    def test_social_messages_get_direct_replies_but_equipment_faults_use_manual_flow(self):
        for question in ('good morning', 'hi how are u', 'how is it going', 'thanks',
                         'who are you', 'what do you do', 'what is your role',
                         'how can you help', 'tell me the use of this agent', 'can you help me?'):
            with self.subTest(question=question):
                self.assertTrue(_smalltalk_reply(question))
        self.assertIsNone(_smalltalk_reply('My machine has low hydraulic pressure.'))

    def test_fallback_does_not_treat_every_short_new_question_as_a_followup(self):
        state={'main_topic':'Saw blade vibration','active_repair':{'issue':'Saw blade vibration',
            'current_step':{'instruction':'Inspect the blade teeth.'}}}
        unrelated=_provider_fallback_plan('There is no hydraulic pressure',state,None)
        self.assertFalse(unrelated.follow_up)
        self.assertEqual(unrelated.topic,'There is no hydraulic pressure')
        self.assertFalse(unrelated.use_previous_photo)
        self.assertTrue(_provider_fallback_plan('Why this check?',state,None).follow_up)
        self.assertTrue(_provider_fallback_plan('The blade is still vibrating',state,None).follow_up)
    def test_photo_survives_text_window_and_is_not_an_assistant_asset(self):
        history = [upload()] + [{'role': 'user', 'text': 'it'} for _ in range(8)]
        history.append({'role': 'assistant', 'image': 'data:image/png;base64,bWFudWFs'})
        self.assertEqual(latest_user_photo(history), PHOTO)
        self.assertFalse(any(m['has_user_photo'] for m in recent_context(history)))

    def test_latest_upload_wins_and_no_cross_session_fallback(self):
        self.assertEqual(latest_user_photo([upload(), upload(NEW_PHOTO)]), NEW_PHOTO)
        self.assertIsNone(latest_user_photo([]))
        self.assertIsNone(latest_user_photo([upload() | {'image': 'data:image/png;base64,!!!'}]))

    def test_planner_receives_actual_previous_photo_and_origin(self):
        client = Mock()
        client.models.generate_content.return_value.text = plan().model_dump_json()
        with patch('app.agent.planner.model_client', return_value=client):
            result = understand('explain about iyt', {}, [upload()], previous_photo=PHOTO)
        parts = client.models.generate_content.call_args.kwargs['contents'].parts
        payload = json.loads(parts[0].text)
        self.assertEqual(payload['photo_origin'], 'previous_upload')
        self.assertTrue(payload['recent_conversation'][0]['has_user_photo'])
        self.assertEqual(parts[1].inline_data.data, PHOTO)
        self.assertTrue(result.use_previous_photo)

    def test_current_upload_supersedes_previous_in_planner(self):
        client = Mock()
        client.models.generate_content.return_value.text = plan().model_dump_json()
        with patch('app.agent.planner.model_client', return_value=client):
            understand('Explain this', {}, [upload()], NEW_PHOTO, PHOTO)
        parts = client.models.generate_content.call_args.kwargs['contents'].parts
        self.assertEqual(json.loads(parts[0].text)['photo_origin'], 'current_upload')
        self.assertEqual(parts[1].inline_data.data, NEW_PHOTO)


class FollowUpTests(unittest.IsolatedAsyncioTestCase):
    async def run_question(self, question, selected_plan, photo=None, history=None):
        entry = SimpleNamespace(id='session', user_id='user', history=history or [upload()])
        sent = []
        async def run_agent(**kwargs):
            sent.append(kwargs['new_message'])
            answer = dict(blocks=[dict(type='text', content='Manual-supported gauge explanation.',
                                      evidence_ids=['evidence-1'])],
                          image_descriptions=[], relevant_asset_ids=[], supported=True, finished=False)
            yield SimpleNamespace(is_final_response=lambda: True,
                                  content=SimpleNamespace(parts=[SimpleNamespace(
                                      text=json.dumps(answer), thought=False)]))

        bundle = {'chunks': [dict(chunk_id='chunk-1', document_name='Manual', page_number=1,
                                  content='Pressure gauge manual evidence')], 'images': [], 'links': []}
        with (
            patch('app.agent.chat_service.store.state', new=AsyncMock(return_value={'main_topic': 'old unrelated topic'})),
            patch('app.agent.chat_service.store.get', new=AsyncMock()),
            patch('app.agent.chat_service.store.update_state', new=AsyncMock()),
            patch('app.agent.chat_service.understand', return_value=selected_plan) as planner,
            patch('app.agent.chat_service.search_manual', return_value=bundle) as search,
            patch('app.agent.chat_service.prepare_media', return_value={'images': [], 'links': [], 'warnings': []}),
            patch('app.agent.chat_service.runner.run_async', side_effect=run_agent),
            patch('app.agent.chat_service.lite_runner.run_async', side_effect=run_agent),
        ):
            response, used_runner = await answer_question(entry, question, photo)
        self.assertTrue(used_runner)
        self.assertIn('gauge explanation', response['blocks'][0]['content'])
        return planner, search, sent[0].parts

    async def test_image_followups_reach_answer_agent_with_photo(self):
        for question in ('explain about iyt', 'it', 'explain about it', 'above image'):
            with self.subTest(question=question):
                planner, search, parts = await self.run_question(question, plan())
                self.assertEqual(planner.call_args.args[-1], PHOTO)
                self.assertEqual(search.call_args.args[0], 'pressure gauge purpose and operation')
                payload = json.loads(parts[0].text)
                self.assertEqual(payload['user_photo_origin'], 'previous_upload')
                self.assertEqual(payload['workflow']['MAIN_TOPIC'], 'pressure gauge')
                self.assertEqual(parts[-1].inline_data.data, PHOTO)

    async def test_unrelated_new_topic_does_not_attach_old_photo(self):
        _, search, parts = await self.run_question('What is the blade speed?', plan(
            query='blade speed', topic='blade speed', follow_up=False, use_previous_photo=False))
        self.assertEqual(search.call_args.args[0], 'blade speed')
        self.assertEqual(len(parts), 1)
        self.assertEqual(json.loads(parts[0].text)['user_photo_origin'], 'none')

    async def test_new_photo_is_used_and_prior_photo_not_offered(self):
        planner, _, parts = await self.run_question('Explain this', plan(
            follow_up=False, use_previous_photo=False), photo=NEW_PHOTO)
        self.assertIsNone(planner.call_args.args[-1])
        self.assertEqual(parts[-1].inline_data.data, NEW_PHOTO)

    async def test_new_photo_forces_new_topic_when_planner_misclassifies_followup(self):
        planner, search, parts = await self.run_question('What is this component?', plan(
            follow_up=True, use_previous_photo=True, query='new component', topic='new component'),
            photo=NEW_PHOTO)
        self.assertEqual(search.call_args.args[0], 'new component')
        self.assertEqual(json.loads(parts[0].text)['workflow']['FOLLOW_UP'], False)

    async def test_plain_text_followup_retains_main_issue(self):
        _, search, parts = await self.run_question('Why?', plan(
            query='why this check', topic='side explanation', use_previous_photo=False),
            history=[{'role': 'user', 'text': 'old unrelated topic'}])
        self.assertEqual(search.call_args.args[0], 'old unrelated topic. why this check')
        self.assertEqual(len(parts), 1)

    async def test_corrected_search_preserves_original_question_for_generation(self):
        examples = (
            ('wat is presure guage functon', 'What is the pressure gauge function?'),
            ('HAB-3232 presure is not 5 bar, explin',
             'HAB-3232 pressure is not 5 bar; explain the pressure indication'),
        )
        for original, resolved in examples:
            with self.subTest(question=original):
                _, search, parts = await self.run_question(original, plan(
                    query=resolved, topic='pressure gauge', follow_up=False,
                    use_previous_photo=False),
                    history=[{'role': 'user', 'text': 'Explain the pressure gauge'}])
                self.assertEqual(search.call_args.args[0], resolved)
                payload = json.loads(parts[0].text)
                self.assertEqual(payload['question'], original)
                self.assertEqual(payload['resolved_query'], resolved)

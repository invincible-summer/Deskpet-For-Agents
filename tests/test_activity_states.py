import json

from agents.codex import CodexFile
from agents.claude import ClaudeFile
from agents.models import Mode, Status, parse_mode, Snapshot
from agents.summarize import shorten, fmt_command
from pet.labels import activity_text


def feed(state, payload, timestamp=100):
    state.feed(json.dumps(dict(type='response_item', timestamp=timestamp, payload=payload)))


def test_codex_new_tool_after_completed_turn_and_long_silence():
    state = CodexFile('unused')
    state.feed(json.dumps(dict(type='event_msg', timestamp=90, payload={'type': 'task_complete'})))
    feed(state, dict(type='function_call', name='exec_command', arguments='{"cmd":"pytest -q"}'))
    obs = state.observation(1000, {})
    assert obs.status == Status.WORKING
    assert 'pytest -q' in obs.summary


def test_codex_question_and_matching_reply():
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='functions.request_user_input', call_id='q', arguments=json.dumps({'questions':[{'question':'选哪个？', 'options':[{'label':'A'}, {'label':'B'}]}]})))
    assert state.observation(1000, {}).status == Status.INPUT
    assert '等待选择回复' in state.observation(1000, {}).summary
    feed(state, dict(type='function_call_output', call_id='other', output='ok'))
    assert state.observation(1000, {}).status == Status.INPUT
    feed(state, dict(type='function_call_output', call_id='q', output='A'))
    assert state.observation(1000, {}).status == Status.WORKING


def test_goal_and_structured_collaboration_mode():
    assert parse_mode({'mode':'plan'})[0] == Mode.PLAN
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='create_goal', arguments='{"objective":"Build"}'))
    assert state.observation(1000, {}).mode == Mode.GOAL
    feed(state, dict(type='function_call', name='update_goal', arguments='{"status":"complete"}'))
    assert state.observation(1000, {}).mode != Mode.GOAL


def test_claude_string_user_and_question():
    state = ClaudeFile('unused')
    state.feed(json.dumps({'type':'user', 'timestamp':100, 'message':{'content':'Fix tests'}}))
    assert state.observation(1000, {}).status == Status.WORKING
    state.feed(json.dumps({'type':'assistant', 'timestamp':101, 'message':{'content':[{'type':'tool_use', 'name':'AskUserQuestion', 'id':'q', 'input':{'questions':[{'question':'Which?', 'options':[{'label':'A'}]}]}}]}}))
    assert state.observation(1000, {}).status == Status.INPUT
    assert 'Which?' in state.observation(1000, {}).summary


def test_bounded_display_preserves_command_syntax():
    for limit in (1, 2, 40, 120):
        assert len(shorten('x' * 200, limit)) <= limit
        assert len(fmt_command('x' * 200, limit)) <= limit
    snap = Snapshot(status=Status.WORKING, summary='rg foo_bar /tmp/my_path')
    assert 'foo_bar /tmp/my_path' in activity_text(snap)
    assert 'foo_bar' not in activity_text(snap, False)

import json
import unittest

from agents.codex import CodexFile
from agents.claude import ClaudeFile
from agents.kimi import KimiFile
from agents.pi import PiFile
from agents.models import AgentKind, Mode, Phase, Status, parse_mode, Snapshot
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
    assert state.observation(1000, {}).goal == 'Build'
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
    snap = Snapshot(key="test", kind=AgentKind.CODEX, source="windows", pid=1, status=Status.WORKING, summary='rg foo_bar /tmp/my_path')
    assert 'foo_bar /tmp/my_path' in activity_text(snap)
    assert 'foo_bar' not in activity_text(snap, False)


def test_codex_error_does_not_resurrect_and_new_input_clears_error():
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='exec_command', arguments='{}'))
    feed(state, dict(type='error', message='failed'), 101)
    assert state.observation(102, {}).status == Status.ERROR
    assert state.observation(200, {}).status == Status.IDLE
    feed(state, dict(type='message', role='user', content=[]), 103)
    assert state.observation(104, {}).status == Status.WORKING
    assert 'exec_command' not in state.observation(104, {}).summary


def test_codex_async_question_survives_ack_until_user_reply():
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='functions.request_user_input_async',
                    call_id='q', arguments=json.dumps({'questions': [{'title': 'Which?', 'options': ['A', 'B']}]})))
    feed(state, dict(type='function_call_output', call_id='q', output='queued'), 101)
    assert state.observation(1000, {}).status == Status.INPUT
    feed(state, dict(type='message', role='user', content=[{'text':'A'}]), 102)
    assert state.observation(103, {}).status == Status.WORKING


def test_claude_question_only_matching_result_or_completion_clears():
    state = ClaudeFile('unused')
    def event(t, **kwargs):
        state.feed(json.dumps(dict(type=t, timestamp=100, **kwargs)))
    def ask():
        event('assistant', message={'content': [{'type':'tool_use', 'name':'AskUserQuestion', 'id':'q', 'input':{'questions':[{'question':'Which?'}]}}]})
    ask()
    event('user', message={'content':[{'type':'tool_result', 'tool_use_id':'other'}]})
    assert state.observation(101, {}).status == Status.INPUT
    event('user', message={'content':[{'type':'tool_result', 'tool_use_id':'q'}]})
    assert state.observation(101, {}).status == Status.WORKING
    ask()
    event('result', result='finished')
    assert state.observation(101, {}).status == Status.DONE


def test_claude_aborted_result_is_error_reminder():
    state = ClaudeFile('unused')
    state.feed(json.dumps({'type': 'assistant', 'timestamp': 100,
                           'message': {'content': [{'type': 'text',
                                                     'text': 'working'}]}}))
    state.feed(json.dumps({'type': 'result', 'timestamp': 101,
                           'subtype': 'aborted', 'result': 'cancelled'}))
    assert state.observation(102, {}).status == Status.ERROR


def test_kimi_turn_cancel_is_error_reminder():
    state = KimiFile('unused')
    state.feed(json.dumps({'type': 'turn.started', 'timestamp': 100}))
    state.feed(json.dumps({'type': 'turn.cancel', 'timestamp': 101}))
    assert state.observation(102, {}).status == Status.ERROR


def test_kimi_scalar_abort_and_codex_structured_abort_are_error_reminders():
    kimi = KimiFile("wire.jsonl")
    kimi.feed(json.dumps({"type": "turn.started", "timestamp": 100}))
    kimi.feed(json.dumps({"type": "turn.ended", "timestamp": 101,
                          "reason": {"kind": "aborted"}}))
    assert kimi.observation(102, {}).status == Status.ERROR

    codex = CodexFile("rollout.jsonl")
    codex.feed(json.dumps({"type": "event_msg", "timestamp": 100,
                           "payload": {"type": "task_started"}}))
    codex.feed(json.dumps({"type": "event_msg", "timestamp": 101,
                           "payload": {"type": "task_complete",
                                       "reason": {"kind": "aborted"}}}))
    assert codex.observation(102, {}).status == Status.ERROR


def test_activity_is_current_fact_without_mode_or_goal_substitution():
    snap = Snapshot(key='test', kind=AgentKind.CODEX, source='windows', pid=1,
                    status=Status.WORKING, mode=Mode.GOAL,
                    summary='rg foo_bar /tmp/my_path ' + 'x' * 200)
    for detail in (False, True):
        assert 'Goal' not in activity_text(snap, detail)
        assert snap.mode is Mode.GOAL
        for limit in (1, 2, 20, 80, 120):
            assert len(activity_text(snap, detail, limit)) <= limit
    snap.goal = '这是用户目标，不能冒充当前命令'
    snap.title = '这只是会话标题'
    for summary in ('', 'Bash', '执行'):
        snap.summary = summary
        assert snap.goal not in activity_text(snap)
        assert snap.title not in activity_text(snap)
        assert 'Bash' not in activity_text(snap)
    snap.summary = '执行：rg live_snapshot agents/monitor.py'
    rendered = activity_text(snap)
    assert rendered.count('：') == 1
    assert 'rg live_snapshot agents/monitor.py' in rendered


def test_codex_wrapper_displays_literal_command_without_executing_code():
    state = CodexFile('unused')
    for code in (
        'text(await tools.exec_command({cmd: "rg foo_bar /tmp/my_path", max_output_tokens: 1000}));',
        "text(await tools.exec_command({workdir: '/work', cmd: 'rg foo_bar /tmp/my_path', max_output_tokens: 1000}));",
    ):
        feed(state, dict(type='custom_tool_call', name='functions.exec', input=code))
        summary = state.observation(1000, {}).summary
        assert 'rg foo_bar /tmp/my_path' in summary
        assert 'max_output_tokens' not in summary
    feed(state, dict(type='message', role='user', content=[{
        'text': '{"objective":"修复监听生命周期","metadata":{"revision":42}}'}]))
    assert state.observation(1000, {}).goal == '修复监听生命周期'


def test_pi_activity_uses_thought_or_tool_input_without_replaying_output():
    state = PiFile('unused')
    def message(role, content, **fields):
        state.feed(json.dumps({'type': 'message', 'timestamp': 100,
                               'message': {'role': role, 'content': content, **fields}}))
    message('user', '修复状态展示')
    message('assistant', [{'type': 'toolCall', 'id': 'call-1', 'name': 'bash',
                           'arguments': {'command': 'rg status agents/'}}],
            stopReason='toolUse')
    obs = state.observation(101, {})
    assert obs.phase is Phase.SEARCHING
    assert 'rg status agents/' in obs.summary
    message('toolResult', [{'type': 'text', 'text': '命令输出不能冒充思考'}],
            toolCallId='call-1', toolName='bash')
    obs = state.observation(101, {})
    assert obs.phase is Phase.THINKING
    assert '命令输出' not in obs.summary
    assert 'rg status' not in obs.summary
    thought = '先检查状态来源，再核对每个解析器。' + '后续细节' * 100
    message('assistant', [{'type': 'thinking', 'thinking': thought}])
    obs = state.observation(101, {})
    assert obs.status is Status.WORKING
    assert obs.phase is Phase.THINKING
    assert obs.summary.startswith('先检查状态来源')
    assert len(obs.summary) <= 160
    message('toolResult', [{'type': 'text', 'text': '迟到的工具输出'}],
            toolCallId='call-1', toolName='bash')
    assert state.observation(101, {}).summary == obs.summary
    message('user', '下一项任务')
    assert '先检查状态来源' not in state.observation(101, {}).summary


def test_kimi_streamed_thought_keeps_its_beginning_in_wire_and_legacy_forms():
    for form in ('wire', 'legacy', 'direct'):
        state = KimiFile('unused')
        def emit(event):
            if form == 'wire':
                obj = {'type': 'context.append_loop_event', 'event': event}
            elif form == 'legacy':
                obj = {'type': event['type'], 'payload': event}
            else:
                obj = event
            state.feed(json.dumps({**obj, 'timestamp': 100}))
        state.feed(json.dumps({'type': 'turn.started', 'timestamp': 99}))
        emit({'type': 'step.begin' if form == 'wire' else 'StepBegin'})
        def thought(text, delta=False):
            if form == 'wire':
                emit({'type': 'content.part', 'part': {'type': 'think', 'think': text}})
            else:
                emit({'type': 'ThinkingDelta' if delta else 'ThinkPart',
                      'delta' if delta else 'think': text})
        thought('先检查')
        thought('取消任务的记录。', True)
        obs = state.observation(101, {})
        assert obs.phase is Phase.THINKING
        assert obs.summary.startswith('先检查取消任务的记录。')
        thought('后续推理' * 200, True)
        stable = state.observation(101, {}).summary
        thought('最后的新尾部', True)
        assert state.observation(101, {}).summary == stable
        assert stable.startswith('先检查取消任务的记录。')
        assert len(stable) <= 160
        emit({'type': 'step.begin' if form == 'wire' else 'StepBegin'})
        thought('新步骤的思考')
        assert state.observation(101, {}).summary == '新步骤的思考'


def test_command_search_and_reasoning_heads_use_actual_activity():
    from agents.models import Phase

    codex = CodexFile('unused')
    command = 'rg *.py agents/ ' + 'beginning_' * 40 + 'NEVER_SHOW_TAIL'
    feed(codex, {'type': 'function_call', 'name': 'exec_command',
                 'arguments': json.dumps({'cmd': command,
                                           'description': '这是目的说明'})})
    observed = codex.observation(101, {})
    snap = Snapshot(key='test', kind=AgentKind.CODEX, source='windows', pid=1,
                    status=observed.status, phase=observed.phase,
                    summary=observed.summary)
    assert activity_text(snap).startswith('搜索中：rg *.py agents/ beginning_')
    assert 'NEVER_SHOW_TAIL' not in activity_text(snap)
    assert '目的说明' not in activity_text(snap)
    codex.feed(json.dumps({'type': 'event_msg', 'timestamp': 102, 'payload': {
        'type': 'exec_command_output_delta', 'delta': '这是输出，不能冒充命令'}}))
    assert codex.observation(103, {}).summary == observed.summary
    for delta in ('先检查', '监听归属。', '后续内容' * 100):
        codex.feed(json.dumps({'type': 'event_msg', 'timestamp': 104,
                              'payload': {'type': 'reasoning_raw_content_delta',
                                          'item_id': 'r1', 'delta': '另一条原始流'}}))
        codex.feed(json.dumps({'type': 'event_msg', 'timestamp': 104,
                              'payload': {'type': 'reasoning_content_delta',
                                          'item_id': 'r1', 'delta': delta}}))
    head = codex.observation(105, {}).summary
    assert head.startswith('先检查监听归属。')
    for _ in range(5):
        codex.feed(json.dumps({'type': 'event_msg', 'timestamp': 106,
                              'payload': {'type': 'reasoning_content_delta',
                                          'item_id': 'r1', 'delta': '尾部内容'}}))
        assert codex.observation(107, {}).summary == head
    feed(codex, {'type': 'web_search_call',
                 'action': {'type': 'search', 'query': 'WSL 会话状态归属'}})
    assert codex.observation(108, {}).phase is Phase.SEARCHING
    assert codex.observation(108, {}).summary == 'WSL 会话状态归属'

    claude = ClaudeFile('unused')
    for block, phase, summary in (
        ({'type': 'thinking', 'thinking': '先检查真实活动证据'},
         Phase.THINKING, '先检查真实活动证据'),
        ({'type': 'tool_use', 'name': 'Bash', 'id': 'tool',
          'input': {'command': 'echo *.py', 'description': '不要显示说明'}},
         Phase.EXECUTING, 'echo *.py'),
        ({'type': 'tool_use', 'name': 'WebSearch', 'id': 'search',
          'input': {'query': '停止任务监听'}}, Phase.SEARCHING, '停止任务监听'),
    ):
        claude.feed(json.dumps({'type': 'assistant', 'timestamp': 109,
                               'message': {'content': [block]}}))
        obs = claude.observation(110, {})
        assert obs.phase is phase
        assert obs.summary == summary


def load_tests(loader, tests, pattern):
    # This repository runs unittest discovery, including these regression functions.
    return unittest.TestSuite(unittest.FunctionTestCase(value)
                              for name, value in sorted(globals().items())
                              if name.startswith('test_') and callable(value))

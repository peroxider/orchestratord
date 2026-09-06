import type { TranslationKey } from './en'

/**
 * 中文文案（动词优先，见 §5.6 文案规范）— must stay in 1:1 key parity with `en`;
 * a missing key is a compile error via `Record<TranslationKey, string>`.
 */
export const zhCN: Record<TranslationKey, string> = {
  'issues.status.queued': '排队中',
  'issues.status.pending': '待处理',
  'issues.status.running': '运行中',
  'issues.status.pending_review': '待评审',
  'issues.status.completed': '已完成',
  'issues.status.failed': '失败',
  'issues.status.abandoned': '已放弃',
  'issues.status.verification_failed': '验证失败',

  'inbox.kind.approval_request': '审批请求',
  'inbox.kind.clarification': '澄清',
  'inbox.kind.failed': '失败',

  'inbox.status.open': '待处理',
  'inbox.status.assigned': '已分配',
  'inbox.status.resolved': '已解决',
  'inbox.status.dismissed': '已忽略',

  'members.role.owner': '所有者',
  'members.role.admin': '管理员',
  'members.role.member': '成员',

  'runtimes.status.online': '在线',
  'runtimes.status.offline': '离线',
  'runtimes.status.disabled': '已禁用',

  'audit.actor.member': '成员',
  'audit.actor.agent': '智能体',
  'audit.actor.system': '系统',

  'usage.dimension.agent': '智能体',
  'usage.dimension.backend': '后端',
  'usage.dimension.issue': '任务',
  'usage.dimension.day': '日',
  'usage.dimension.workspace': '工作区',

  'events.kind.text': '文本',
  'events.kind.text_delta': '文本增量',
  'events.kind.tool_call': '工具调用',
  'events.kind.tool_result': '工具结果',
  'events.kind.turn_complete': '回合完成',
  'events.kind.phase_complete': '阶段完成',
  'events.kind.session_complete': '会话完成',
  'events.kind.error': '错误',
  'events.kind.goal_set': '目标已设置',
  'events.kind.goal_status': '目标状态',
  'events.kind.goal_continue': '目标继续',
  'events.kind.goal_done': '目标完成',
  'events.kind.goal_cleared': '目标已清除',
  'events.kind.goal_paused': '目标已暂停',
  'events.kind.approval_request': '审批请求',
  'events.kind.unknown': '未知',

  'events.summary.tool_result': '工具结果',
  'events.summary.approval_request': '审批请求',
  'events.summary.error': '错误',

  'agents.capability.streaming_deltas': '流式增量',
  'agents.capability.resumable': '可恢复',
  'agents.capability.interrupt': '可中断',
  'agents.capability.approval_hooks': '审批钩子',
  'agents.capability.parallel_sessions': '并行会话',
  'agents.capability.cost_reporting': '成本报告',
  'agents.capability.tool_filtering': '工具过滤',
  'agents.capability.takeover': '接管',
  'agents.capability.goal_mode': '目标模式',
  'agents.capability.resume_detection': '恢复检测',

  'vcs.pr_state.open': '开启',
  'vcs.pr_state.merged': '已合并',
  'vcs.pr_state.closed': '已关闭',
}

import type { Locale } from '../i18n'

export const modeCopy = {
  en: { pipelineEmpty: 'No pipeline events yet.', pipelineLabel: 'Pipeline stages', contextInjected: 'Context injected into next stage', events: 'events', event: 'event', last: 'last', debateEmpty: 'No debate events yet.', debateLabel: 'Debate participants', noProposers: 'No proposer events yet — the judge appears when judging starts.', independent: 'Independent reasoning — proposers did not see each other', judgePending: 'Judge pending — runs after both proposers complete.', lens: 'Lens', judge: 'Judge', judgeNote: "Saw both proposers' outputs verbatim and implemented the winner.", swarmEmpty: 'No swarm events yet.', swarmLabel: 'Swarm execution waves', wave: 'Wave', done: 'done', running: 'running', pending: 'pending', failed: 'failed', coordinatorEmpty: 'No coordinator events yet.', coordinatorLabel: 'Coordinator task distribution', task: 'Task', timeline: 'Timeline' },
  'zh-CN': { pipelineEmpty: '暂无流水线事件。', pipelineLabel: '流水线阶段', contextInjected: '上下文已传递到下一阶段', events: '个事件', event: '个事件', last: '最近事件', debateEmpty: '暂无辩论事件。', debateLabel: '辩论参与者', noProposers: '暂无提案事件；评审开始后将显示 Judge。', independent: '独立推理——提案方无法看到彼此的输出', judgePending: 'Judge 正在等待，将在双方提案完成后运行。', lens: '视角', judge: 'Judge', judgeNote: '已查看双方提案的完整输出，并执行胜出方案。', swarmEmpty: '暂无群体执行事件。', swarmLabel: '群体执行波次', wave: '波次', done: '已完成', running: '运行中', pending: '待处理', failed: '失败', coordinatorEmpty: '暂无协调器事件。', coordinatorLabel: '协调器任务分布', task: '任务', timeline: '时间线' },
  ja: { pipelineEmpty: 'パイプラインイベントはまだありません。', pipelineLabel: 'パイプラインステージ', contextInjected: '次のステージへコンテキストを渡しました', events: '件のイベント', event: '件のイベント', last: '最新', debateEmpty: 'ディベートイベントはまだありません。', debateLabel: 'ディベート参加者', noProposers: '提案イベントはまだありません。審査開始後に Judge を表示します。', independent: '独立推論 — 提案者同士は互いの出力を参照していません', judgePending: 'Judge は待機中です。両方の提案完了後に実行します。', lens: '観点', judge: 'Judge', judgeNote: '両方の提案を完全に確認し、採用案を実装しました。', swarmEmpty: 'Swarm イベントはまだありません。', swarmLabel: 'Swarm 実行ウェーブ', wave: 'ウェーブ', done: '完了', running: '実行中', pending: '待機中', failed: '失敗', coordinatorEmpty: 'コーディネーターイベントはまだありません。', coordinatorLabel: 'コーディネーターのタスク分布', task: 'タスク', timeline: 'タイムライン' },
} as const

const statusCopy = {
  en: { running: 'Running', in_progress: 'In progress', completed: 'Completed', failed: 'Failed', todo: 'To do', pending: 'Pending', unknown: 'Unknown' },
  'zh-CN': { running: '运行中', in_progress: '进行中', completed: '已完成', failed: '失败', todo: '待办', pending: '待处理', unknown: '未知' },
  ja: { running: '実行中', in_progress: '進行中', completed: '完了', failed: '失敗', todo: '未着手', pending: '保留中', unknown: '不明' },
} as const

export function modeStatusLabel(status: string, locale: Locale): string {
  return statusCopy[locale][status as keyof typeof statusCopy.en] ?? statusCopy[locale].unknown
}

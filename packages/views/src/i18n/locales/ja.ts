import type { TranslationKey } from './en'

/**
 * 日本語コピー — must stay in 1:1 key parity with `en` (§9.2 三言語);
 * a missing key is a compile error via `Record<TranslationKey, string>`.
 */
export const ja: Record<TranslationKey, string> = {
  'issues.status.queued': 'キュー待ち',
  'issues.status.pending': '未処理',
  'issues.status.running': '実行中',
  'issues.status.pending_review': 'レビュー待ち',
  'issues.status.completed': '完了',
  'issues.status.failed': '失敗',
  'issues.status.abandoned': '中止',
  'issues.status.verification_failed': '検証失敗',

  'inbox.kind.approval_request': '承認リクエスト',
  'inbox.kind.clarification': '確認質問',
  'inbox.kind.failed': '失敗',

  'inbox.status.open': '未対応',
  'inbox.status.assigned': '割り当て済み',
  'inbox.status.resolved': '解決済み',
  'inbox.status.dismissed': '却下',

  'inbox.action.assign_me': '自分に割り当て',
  'inbox.action.approve': '承認',
  'inbox.action.reject': '却下',
  'inbox.action.mark_answered': '回答済みにする',
  'inbox.action.mark_handled': '処理済みにする',
  'inbox.action.dismiss': '却下',

  'inbox.hint.approval_request': 'エージェントが承認を待っており、続行できません。',
  'inbox.hint.clarification': 'エージェントが確認質問をしました。',
  'inbox.hint.failed': 'セッションの実行が失敗しました。対応が必要です。',

  'runtimes.status.online': 'オンライン',
  'runtimes.status.offline': 'オフライン',
  'runtimes.status.disabled': '無効',

  'audit.actor.member': 'ローカル操作者',
  'audit.actor.agent': 'エージェント',
  'audit.actor.system': 'システム',

  'usage.dimension.agent': 'エージェント',
  'usage.dimension.backend': 'バックエンド',
  'usage.dimension.issue': 'Issue',
  'usage.dimension.day': '日',
  'usage.dimension.workspace': 'インスタンス',

  'usage.metric.tokens_total': '合計トークン',
  'usage.metric.cost_usd': 'コスト (USD)',
  'usage.metric.sessions': 'セッション数',

  'events.kind.text': 'テキスト',
  'events.kind.text_delta': 'テキスト差分',
  'events.kind.tool_call': 'ツール呼び出し',
  'events.kind.tool_result': 'ツール結果',
  'events.kind.turn_complete': 'ターン完了',
  'events.kind.phase_complete': 'フェーズ完了',
  'events.kind.session_complete': 'セッション完了',
  'events.kind.error': 'エラー',
  'events.kind.goal_set': '目標設定済み',
  'events.kind.goal_status': '目標ステータス',
  'events.kind.goal_continue': '目標継続',
  'events.kind.goal_done': '目標完了',
  'events.kind.goal_cleared': '目標クリア',
  'events.kind.goal_paused': '目標一時停止',
  'events.kind.approval_request': '承認リクエスト',
  'events.kind.unknown': '不明',

  'events.summary.tool_result': 'ツール結果',
  'events.summary.approval_request': '承認リクエスト',
  'events.summary.error': 'エラー',

  'agents.capability.streaming_deltas': 'ストリーミング差分',
  'agents.capability.resumable': '再開可能',
  'agents.capability.interrupt': '割り込み可能',
  'agents.capability.approval_hooks': '承認フック',
  'agents.capability.parallel_sessions': '並行セッション',
  'agents.capability.cost_reporting': 'コストレポート',
  'agents.capability.tool_filtering': 'ツールフィルタ',
  'agents.capability.takeover': 'テイクオーバー',
  'agents.capability.goal_mode': '目標モード',
  'agents.capability.resume_detection': '再開検出',

  'vcs.pr_state.open': 'オープン',
  'vcs.pr_state.merged': 'マージ済み',
  'vcs.pr_state.closed': 'クローズ',
}

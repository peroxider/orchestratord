'use client'

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  type ReactNode,
} from 'react'
import { usePathname, useRouter } from 'next/navigation'
import {
  useCreateIssue,
  useInbox,
  useInstance,
  useRealtimeBridge,
  useSessions,
  type InstanceBootstrap,
} from '@orchestratord/core'
import { Button, Input, Textarea } from '@orchestratord/ui'
import { LocaleSwitcher, useLocale } from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { useTheme } from '@/app/web-providers'

const InstanceContext = createContext<InstanceBootstrap | null>(null)

export function useInstanceContext() {
  const value = useContext(InstanceContext)
  if (!value) throw new Error('useInstanceContext must be used inside AppShell')
  return value
}

type IconName =
  | 'overview' | 'inbox' | 'chat' | 'issues' | 'projects' | 'sessions'
  | 'agents' | 'squads' | 'autopilots' | 'runtimes' | 'skills' | 'usage'
  | 'activity' | 'search' | 'plus' | 'menu' | 'sun' | 'moon' | 'close' | 'help'

const ICON_PATHS: Record<IconName, ReactNode> = {
  overview: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 17h7M17.5 13.5v7"/></>,
  inbox: <><path d="M4 4h16v15H4z"/><path d="M4 14h4l2 3h4l2-3h4"/></>,
  chat: <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/>,
  issues: <><circle cx="12" cy="12" r="9"/><path d="M9 12l2 2 4-5"/></>,
  projects: <><path d="M3 6h7l2 2h9v11H3z"/><path d="M3 6V4h7l2 2"/></>,
  sessions: <><path d="M4 19V5"/><circle cx="4" cy="5" r="2"/><circle cx="4" cy="19" r="2"/><path d="M6 8h8a4 4 0 0 1 4 4v3"/><circle cx="18" cy="18" r="3"/></>,
  agents: <><rect x="4" y="7" width="16" height="13" rx="3"/><path d="M9 12h.01M15 12h.01M9 16h6M12 7V3M9 3h6"/></>,
  squads: <><circle cx="9" cy="8" r="3"/><circle cx="17" cy="10" r="2"/><path d="M3 20a6 6 0 0 1 12 0M14 16a5 5 0 0 1 7 4"/></>,
  autopilots: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2M8 2l-2 3M16 2l2 3"/></>,
  runtimes: <><rect x="3" y="4" width="18" height="6" rx="1"/><rect x="3" y="14" width="18" height="6" rx="1"/><path d="M7 7h.01M7 17h.01M11 7h7M11 17h7"/></>,
  skills: <><path d="M12 3l2.2 4.5L19 8l-3.5 3.3.9 4.7-4.4-2.3L7.6 16l.9-4.7L5 8l4.8-.5z"/><path d="M4 20h16"/></>,
  usage: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></>,
  activity: <><path d="M3 12h4l2-6 4 12 2-6h6"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="M16 16l5 5"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  menu: <path d="M4 7h16M4 12h16M4 17h16"/>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></>,
  moon: <path d="M20 15.5A8.5 8.5 0 0 1 8.5 4 8.5 8.5 0 1 0 20 15.5z"/>,
  close: <path d="M6 6l12 12M18 6L6 18"/>,
  help: <><circle cx="12" cy="12" r="9"/><path d="M9.8 9a2.4 2.4 0 1 1 3.7 2c-1 .6-1.5 1.1-1.5 2.2M12 17h.01"/></>,
}

function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return <svg className="app-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{ICON_PATHS[name]}</svg>
}

const copy = {
  en: { groups: ['Attention', 'Work', 'Orchestration', 'System'], newIssue: 'New issue', search: 'Search or run a command', connected: 'Connected', connecting: 'Connecting', reconnecting: 'Reconnecting', degraded: 'Degraded', offline: 'Offline', create: 'Create issue', title: 'Issue title', description: 'Describe the desired outcome…', cancel: 'Cancel', offlineNote: 'Realtime is offline. Existing data is safe; reconnect the local service to receive live updates.', reconnectNote: 'Realtime is taking longer than expected to reconnect. You can keep working with the data already loaded.', degradedNote: 'Live updates are degraded. Loaded data remains available while the console continues reconnecting.', recovered: 'Realtime connection restored.', noResults: 'No matching issues, sessions, agents, or commands.', creating: 'Creating…' },
  'zh-CN': { groups: ['需要关注', '工作', '编排', '系统'], newIssue: '新建任务', search: '搜索或运行命令', connected: '已连接', connecting: '连接中', reconnecting: '正在重连', degraded: '服务降级', offline: '离线', create: '创建任务', title: '任务标题', description: '描述期望结果…', cancel: '取消', offlineNote: '实时连接已离线。现有数据不会受影响；重新连接本地服务后即可接收更新。', reconnectNote: '实时连接恢复时间超出预期。你仍可继续处理已加载的数据。', degradedNote: '实时更新已降级。已加载数据仍可使用，控制台会继续尝试重连。', recovered: '实时连接已恢复。', noResults: '没有匹配的任务、会话、Agent 或命令。', creating: '正在创建…' },
  ja: { groups: ['要対応', '作業', 'オーケストレーション', 'システム'], newIssue: 'Issue を作成', search: '検索またはコマンド', connected: '接続済み', connecting: '接続中', reconnecting: '再接続中', degraded: '機能低下', offline: 'オフライン', create: 'Issue を作成', title: 'Issue タイトル', description: '期待する結果を説明…', cancel: 'キャンセル', offlineNote: 'リアルタイム接続がオフラインです。既存データは安全です。ローカルサービスを再接続すると更新を受信できます。', reconnectNote: 'リアルタイム接続の復旧に時間がかかっています。読み込み済みのデータは引き続き操作できます。', degradedNote: 'ライブ更新の機能が低下しています。読み込み済みデータは利用でき、再接続を継続します。', recovered: 'リアルタイム接続が復旧しました。', noResults: '一致する Issue、セッション、エージェント、コマンドはありません。', creating: '作成中…' },
} as const

const NAV = [
  [{ href: '/', icon: 'overview', labels: ['Overview', '总览', '概要'] }, { href: '/inbox', icon: 'inbox', labels: ['Inbox', '收件箱', '受信箱'] }, { href: '/chat', icon: 'chat', labels: ['Chat', '对话', 'チャット'] }],
  [{ href: '/issues', icon: 'issues', labels: ['Issues', '任务', 'Issue'] }, { href: '/projects', icon: 'projects', labels: ['Projects', '项目', 'プロジェクト'] }],
  [{ href: '/sessions', icon: 'sessions', labels: ['Sessions', '会话', 'セッション'] }, { href: '/agents', icon: 'agents', labels: ['Agents', 'Agent', 'エージェント'] }, { href: '/squads', icon: 'squads', labels: ['Squads', 'Agent 小组', 'Squad'] }, { href: '/autopilots', icon: 'autopilots', labels: ['Autopilots', '自动任务', '自動タスク'] }],
  [{ href: '/runtimes', icon: 'runtimes', labels: ['Runtimes', '运行时', 'ランタイム'] }, { href: '/skills', icon: 'skills', labels: ['Skills', '技能', 'スキル'] }, { href: '/usage', icon: 'usage', labels: ['Usage', '用量', '使用量'] }, { href: '/activity', icon: 'activity', labels: ['Activity', '活动', 'アクティビティ'] }],
] as const

const detailsCopy = {
  en: { help: 'Keyboard shortcuts', diagnostics: 'Connection diagnostics', rest: 'REST API', realtime: 'Realtime', runtime: 'Runtime target', status: 'Status', local: 'Local instance', close: 'Close', nav: 'Navigate', shortcuts: [['⌘/Ctrl K', 'Search and commands'], ['C', 'Create an issue'], ['G then O', 'Go to Overview'], ['G then I', 'Go to Inbox'], ['G then W', 'Go to Issues'], ['G then S', 'Go to Sessions'], ['?', 'Open shortcut help'], ['Esc', 'Close the top layer']] },
  'zh-CN': { help: '键盘快捷键', diagnostics: '连接诊断', rest: 'REST API', realtime: '实时连接', runtime: '运行时目标', status: '状态', local: '本地实例', close: '关闭', nav: '导航', shortcuts: [['⌘/Ctrl K', '搜索与命令'], ['C', '创建任务'], ['G 再按 O', '前往总览'], ['G 再按 I', '前往收件箱'], ['G 再按 W', '前往任务'], ['G 再按 S', '前往会话'], ['?', '打开快捷键帮助'], ['Esc', '关闭最上层界面']] },
  ja: { help: 'キーボードショートカット', diagnostics: '接続診断', rest: 'REST API', realtime: 'リアルタイム', runtime: 'ランタイム接続先', status: '状態', local: 'ローカルインスタンス', close: '閉じる', nav: '移動', shortcuts: [['⌘/Ctrl K', '検索とコマンド'], ['C', 'Issue を作成'], ['G → O', '概要へ移動'], ['G → I', '受信箱へ移動'], ['G → W', 'Issue へ移動'], ['G → S', 'セッションへ移動'], ['?', 'ショートカットを表示'], ['Esc', '最前面を閉じる']] },
} as const

const GO_ROUTES: Record<string, string> = { o: '/', i: '/inbox', c: '/chat', w: '/issues', p: '/projects', s: '/sessions', a: '/agents', q: '/squads', u: '/autopilots', r: '/runtimes', k: '/skills', g: '/usage', v: '/activity' }
type CommandItem = { label: string; type: string; href: string }
const RECENT_KEY = 'orchestratord.recent-items'

function recentItems(): CommandItem[] {
  if (typeof window === 'undefined') return []
  try {
    const value = JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]')
    return Array.isArray(value) ? value.filter(item => item && typeof item.label === 'string' && typeof item.type === 'string' && typeof item.href === 'string').slice(0, 6) : []
  } catch { return [] }
}

function rememberItem(item: CommandItem): CommandItem[] {
  const next = [item, ...recentItems().filter(value => value.href !== item.href)].slice(0, 6)
  localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  return next
}

const paletteCopy = {
  en: { recent: 'Recent', browse: 'browse', open: 'open', newIssue: 'new issue', close: 'close', palette: 'Command palette' },
  'zh-CN': { recent: '最近访问', browse: '选择', open: '打开', newIssue: '新建任务', close: '关闭', palette: '命令面板' },
  ja: { recent: '最近使った項目', browse: '選択', open: '開く', newIssue: 'Issue を作成', close: '閉じる', palette: 'コマンドパレット' },
} as const

function localeIndex(locale: string) { return locale === 'zh-CN' ? 1 : locale === 'ja' ? 2 : 0 }

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

function useDialogFocus<T extends HTMLElement>(ref: RefObject<T | null>, returnFocus?: HTMLElement | null) {
  useEffect(() => {
    const previous = returnFocus ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null)
    const frame = window.requestAnimationFrame(() => {
      const first = ref.current?.querySelector<HTMLElement>(FOCUSABLE)
      if (first && !ref.current?.contains(document.activeElement)) first.focus()
    })
    return () => {
      window.cancelAnimationFrame(frame)
      if (previous?.isConnected) previous.focus()
    }
  }, [ref, returnFocus])
  const onKeyDown = (event: ReactKeyboardEvent<T>) => {
    if (event.key !== 'Tab' || !ref.current) return
    const items = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
    if (items.length === 0) return
    const first = items[0]!
    const last = items[items.length - 1]!
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }
  return onKeyDown
}

export function AppShell({ children }: { children: ReactNode }) {
  const instance = useInstance(apiClient)
  if (instance.isPending) return <BootstrapState />
  if (instance.isError) return <BootstrapState error={instance.error?.message} retry={() => instance.refetch()} />
  return <InstanceContext.Provider value={instance.data}><ShellContent instance={instance.data}>{children}</ShellContent></InstanceContext.Provider>
}

function BootstrapState({ error, retry }: { error?: string; retry?: () => void }) {
  return <main className="bootstrap-state"><div className="brand-mark">O</div><p className="bootstrap-state__eyebrow">LOCAL CONTROL PLANE</p><h1>{error ? 'Instance unavailable' : 'Opening execution ledger'}</h1><p>{error ? `The local API could not initialize the console. Your data has not been changed. ${error}` : 'Resolving the local instance and execution context…'}</p>{retry && <Button onClick={retry}>Retry connection</Button>}</main>
}

function ShellContent({ instance, children }: { instance: InstanceBootstrap; children: ReactNode }) {
  const pathname = usePathname() ?? '/'
  const router = useRouter()
  const locale = useLocale()
  const c = copy[locale]
  const idx = localeIndex(locale)
  const { theme, toggleTheme } = useTheme()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(() => typeof window !== 'undefined' && localStorage.getItem('orchestratord.sidebar') === 'collapsed')
  const [searchOpen, setSearchOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [infoOpen, setInfoOpen] = useState<'help' | 'diagnostics' | null>(null)
  const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null)
  const goPrefix = useRef<number | null>(null)
  const [connection, setConnection] = useState<'connecting' | 'connected' | 'reconnecting' | 'degraded' | 'offline'>('connecting')
  const [reconnectNotice, setReconnectNotice] = useState(false)
  const [recovered, setRecovered] = useState(false)
  const everConnected = useRef(false)
  const reconnectTimer = useRef<number | null>(null)
  const degradedTimer = useRef<number | null>(null)
  const recoveredTimer = useRef<number | null>(null)
  const inbox = useInbox(apiClient, instance.workspace_id)
  const sessions = useSessions(apiClient, instance.workspace_id)
  const inboxCount = (inbox.data ?? []).filter(item => item.status === 'open' || item.status === 'assigned').length
  const activeSessionCount = (sessions.data ?? []).filter(item => ['pending', 'queued', 'running', 'paused', 'waiting'].includes(item.status)).length
  const onRealtimeStatus = useCallback((status: 'connecting' | 'open' | 'closed' | 'error') => {
    if (status === 'open') {
      if (reconnectTimer.current !== null) window.clearTimeout(reconnectTimer.current)
      if (degradedTimer.current !== null) window.clearTimeout(degradedTimer.current)
      reconnectTimer.current = null
      degradedTimer.current = null
      setReconnectNotice(false)
      if (everConnected.current) {
        setRecovered(true)
        if (recoveredTimer.current !== null) window.clearTimeout(recoveredTimer.current)
        recoveredTimer.current = window.setTimeout(() => setRecovered(false), 3_000)
      }
      everConnected.current = true
      setConnection('connected')
      return
    }
    if (status === 'connecting') {
      if (!everConnected.current) { setConnection('connecting'); return }
      setConnection(current => current === 'degraded' ? current : 'reconnecting')
      if (reconnectTimer.current === null) reconnectTimer.current = window.setTimeout(() => setReconnectNotice(true), 5_000)
      if (degradedTimer.current === null) degradedTimer.current = window.setTimeout(() => setConnection('degraded'), 15_000)
      return
    }
    if (everConnected.current && window.navigator.onLine) {
      setConnection(current => current === 'degraded' ? current : 'reconnecting')
      if (reconnectTimer.current === null) reconnectTimer.current = window.setTimeout(() => setReconnectNotice(true), 5_000)
      if (degradedTimer.current === null) degradedTimer.current = window.setTimeout(() => setConnection('degraded'), 15_000)
      return
    }
    if (reconnectTimer.current !== null) window.clearTimeout(reconnectTimer.current)
    reconnectTimer.current = null
    if (degradedTimer.current !== null) window.clearTimeout(degradedTimer.current)
    degradedTimer.current = null
    setReconnectNotice(false)
    setConnection('offline')
  }, [])
  useRealtimeBridge({ url: instance.realtime_url, workspaceId: instance.workspace_id, onStatus: onRealtimeStatus })

  useEffect(() => () => {
    if (reconnectTimer.current !== null) window.clearTimeout(reconnectTimer.current)
    if (degradedTimer.current !== null) window.clearTimeout(degradedTimer.current)
    if (recoveredTimer.current !== null) window.clearTimeout(recoveredTimer.current)
  }, [])

  const openSearch = useCallback(() => {
    setReturnFocus(document.activeElement instanceof HTMLElement ? document.activeElement : null)
    setSearchOpen(true)
  }, [])
  const openCreate = useCallback(() => {
    setReturnFocus(document.activeElement instanceof HTMLElement ? document.activeElement : null)
    setCreateOpen(true)
  }, [])
  const openInfo = useCallback((kind: 'help' | 'diagnostics') => {
    setReturnFocus(document.activeElement instanceof HTMLElement ? document.activeElement : null)
    setInfoOpen(kind)
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement
      const key = event.key.toLowerCase()
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openSearch() }
      if (!typing && !event.metaKey && !event.ctrlKey && goPrefix.current !== null && GO_ROUTES[key]) { event.preventDefault(); window.clearTimeout(goPrefix.current); goPrefix.current = null; router.push(GO_ROUTES[key]); setMobileOpen(false); return }
      if (!typing && !event.metaKey && !event.ctrlKey && key === 'g') { event.preventDefault(); if (goPrefix.current !== null) window.clearTimeout(goPrefix.current); goPrefix.current = window.setTimeout(() => { goPrefix.current = null }, 1_200); return }
      if (!typing && !event.metaKey && !event.ctrlKey && key === 'c') { event.preventDefault(); openCreate() }
      if (!typing && !event.metaKey && !event.ctrlKey && event.key === '?') { event.preventDefault(); openInfo('help') }
      if (event.key === 'Escape') { setSearchOpen(false); setCreateOpen(false); setInfoOpen(null); setMobileOpen(false) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [openCreate, openInfo, openSearch, router])

  const active = NAV.flat().find((item) => item.href === '/' ? pathname === '/' : pathname.startsWith(item.href)) ?? NAV[0][0]
  const title = active.labels[idx]
  const connectionText = connection === 'connected' ? c.connected : connection === 'connecting' ? c.connecting : connection === 'reconnecting' ? c.reconnecting : connection === 'degraded' ? c.degraded : c.offline
  const navigate = (href: string) => {
    const item = NAV.flat().find(value => value.href === href)
    if (item) rememberItem({ label: item.labels[idx], type: 'Navigate', href })
    router.push(href); setMobileOpen(false)
  }

  return <div className={`app-shell${collapsed ? ' app-shell--collapsed' : ''}`}>
    {mobileOpen && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}
    <aside className={`app-sidebar${mobileOpen ? ' app-sidebar--open' : ''}`}>
      <div className="app-sidebar__brand"><span className="brand-mark">O</span><span className="app-sidebar__brand-copy"><strong>{instance.instance_name}</strong><small>EXECUTION LEDGER</small></span><button className="icon-button app-sidebar__mobile-close" aria-label="Close navigation" onClick={() => setMobileOpen(false)}><Icon name="close" /></button></div>
      <nav className="app-nav" aria-label="Primary navigation">
        {NAV.map((group, groupIndex) => <section className="app-nav__group" key={c.groups[groupIndex]}><p className="app-nav__label">{c.groups[groupIndex]}</p>{group.map((item) => { const selected = item.href === '/' ? pathname === '/' : pathname.startsWith(item.href); const count = item.href === '/inbox' ? inboxCount : item.href === '/sessions' ? activeSessionCount : 0; return <button key={item.href} className="app-nav__item" data-selected={selected || undefined} onClick={() => navigate(item.href)} title={item.labels[idx]}><Icon name={item.icon} /><span>{item.labels[idx]}</span>{count > 0 && <i className="app-nav__badge">{count}</i>}{item.href === '/runtimes' && connection !== 'connected' && <i className={`nav-connection-dot connection-dot connection-dot--${connection}`} aria-hidden="true" />}</button> })}</section>)}
      </nav>
      <div className="app-sidebar__footer"><button className="runtime-summary" onClick={() => openInfo('diagnostics')}><span className={`connection-dot connection-dot--${connection}`} /><div><strong>{connectionText}</strong><small>localhost · v{instance.server_version}</small></div></button><button className="icon-button sidebar-help" onClick={() => openInfo('help')} aria-label={detailsCopy[locale].help}><Icon name="help" /></button><button className="sidebar-collapse" onClick={() => { const next = !collapsed; setCollapsed(next); localStorage.setItem('orchestratord.sidebar', next ? 'collapsed' : 'expanded') }} aria-label="Toggle sidebar">{collapsed ? '›' : '‹'}</button></div>
    </aside>
    <div className="app-main">
      <header className="page-header"><div className="page-header__identity"><button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileOpen(true)}><Icon name="menu" /></button><Icon name={active.icon} /><h1>{title}</h1></div><div className="page-header__actions"><button className="search-trigger" onClick={openSearch}><Icon name="search" /><span>{c.search}</span><kbd>⌘ K</kbd></button><button className="connection-button" onClick={() => openInfo('diagnostics')} title={`${instance.realtime_url} · REST 127.0.0.1:9000`}><span className={`connection-dot connection-dot--${connection}`} /><span>{connectionText}</span></button><button className="icon-button" aria-label="Toggle theme" onClick={toggleTheme}><Icon name={theme === 'dark' ? 'sun' : 'moon'} /></button><LocaleSwitcher /><Button onClick={openCreate}><Icon name="plus" />{c.newIssue}</Button></div></header>
      {connection === 'offline' && <div className="connection-banner">{c.offlineNote}</div>}
      {reconnectNotice && (connection === 'reconnecting' || connection === 'degraded') && <div className="connection-banner">{connection === 'degraded' ? c.degradedNote : c.reconnectNote}</div>}
      <main className="page-canvas">{children}</main>
    </div>
    {searchOpen && <CommandPalette instance={instance} close={() => setSearchOpen(false)} navigate={navigate} returnFocus={returnFocus} />}
    {createOpen && <CreateIssueDialog workspaceId={instance.workspace_id} close={() => setCreateOpen(false)} labels={c} navigate={navigate} returnFocus={returnFocus} />}
    {infoOpen && <InfoDialog kind={infoOpen} instance={instance} connection={connection} close={() => setInfoOpen(null)} returnFocus={returnFocus} />}
    {recovered && <div className="sync-toast" role="status"><span className="connection-dot connection-dot--connected" />{c.recovered}</div>}
  </div>
}

function InfoDialog({ kind, instance, connection, close, returnFocus }: { kind: 'help' | 'diagnostics'; instance: InstanceBootstrap; connection: string; close: () => void; returnFocus?: HTMLElement | null }) {
  const locale = useLocale(); const c = detailsCopy[locale]; const ref = useRef<HTMLElement>(null); const onKeyDown = useDialogFocus(ref, returnFocus)
  return <div className="dialog-layer" onMouseDown={e => { if (e.target === e.currentTarget) close() }}><section ref={ref} onKeyDown={onKeyDown} className="info-dialog" role="dialog" aria-modal="true" aria-label={kind === 'help' ? c.help : c.diagnostics}><header><div><p className="dialog-eyebrow">{kind === 'help' ? c.nav : c.local}</p><h2>{kind === 'help' ? c.help : c.diagnostics}</h2></div><button autoFocus type="button" className="icon-button" aria-label={c.close} onClick={close}><Icon name="close" /></button></header>{kind === 'help' ? <dl className="shortcut-list">{c.shortcuts.map(([keys, label]) => <div key={keys}><dt><kbd>{keys}</kbd></dt><dd>{label}</dd></div>)}</dl> : <dl className="diagnostic-list"><div><dt>{c.status}</dt><dd><span className={`connection-dot connection-dot--${connection}`} />{connection}</dd></div><div><dt>{c.rest}</dt><dd><code>http://127.0.0.1:9000</code></dd></div><div><dt>{c.realtime}</dt><dd><code>{instance.realtime_url}</code></dd></div><div><dt>{c.runtime}</dt><dd>localhost · v{instance.server_version}</dd></div></dl>}</section></div>
}

function CommandPalette({ instance, close, navigate, returnFocus }: { instance: InstanceBootstrap; close: () => void; navigate: (href: string) => void; returnFocus?: HTMLElement | null }) {
  const locale = useLocale(); const idx = localeIndex(locale)
  const p = paletteCopy[locale]
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const [recent, setRecent] = useState<CommandItem[]>(recentItems)
  const dialogRef = useRef<HTMLElement>(null)
  const onDialogKeyDown = useDialogFocus(dialogRef, returnFocus)
  const [entities, setEntities] = useState<CommandItem[]>([])
  useEffect(() => {
    let live = true
    Promise.allSettled([
      apiClient.request<Array<{ id: string; title: string }>>(`/api/workspaces/${instance.workspace_id}/issues`),
      apiClient.request<Array<{ id: string; mode: string }>>(`/api/workspaces/${instance.workspace_id}/sessions`),
      apiClient.request<Array<{ id: string; name: string }>>(`/api/workspaces/${instance.workspace_id}/agents`),
      apiClient.request<Array<{ id: string; name: string }>>(`/api/workspaces/${instance.workspace_id}/projects`),
      apiClient.request<Array<{ id: string; hostname: string }>>(`/api/workspaces/${instance.workspace_id}/runtimes`),
      apiClient.request<Array<{ name: string; display_name: string }>>('/api/skills'),
      apiClient.request<Array<{ id: string; name: string }>>(`/api/workspaces/${instance.workspace_id}/autopilots`),
      apiClient.request<Array<{ id: string; name: string }>>(`/api/workspaces/${instance.workspace_id}/squads`),
    ]).then(([issues, sessions, agents, projects, runtimes, skills, autopilots, squads]) => { if (!live) return; setEntities([
      ...(issues.status === 'fulfilled' ? issues.value.map(x => ({ label: x.title, type: 'Issue', href: `/issues/${x.id}` })) : []),
      ...(sessions.status === 'fulfilled' ? sessions.value.map(x => ({ label: `${x.mode} · ${x.id.slice(0, 8)}`, type: 'Session', href: `/sessions/${x.id}` })) : []),
      ...(agents.status === 'fulfilled' ? agents.value.map(x => ({ label: x.name, type: 'Agent', href: `/agents/${x.id}` })) : []),
      ...(projects.status === 'fulfilled' ? projects.value.map(x => ({ label: x.name, type: 'Project', href: `/projects/${x.id}` })) : []),
      ...(runtimes.status === 'fulfilled' ? runtimes.value.map(x => ({ label: x.hostname, type: 'Runtime', href: `/runtimes/${x.id}` })) : []),
      ...(skills.status === 'fulfilled' ? skills.value.map(x => ({ label: x.display_name, type: 'Skill', href: `/skills/${encodeURIComponent(x.name)}` })) : []),
      ...(autopilots.status === 'fulfilled' ? autopilots.value.map(x => ({ label: x.name, type: 'Autopilot', href: `/autopilots/${x.id}` })) : []),
      ...(squads.status === 'fulfilled' ? squads.value.map(x => ({ label: x.name, type: 'Squad', href: `/squads/${x.id}` })) : []),
    ]) })
    return () => { live = false }
  }, [instance.workspace_id])
  const navItems: CommandItem[] = NAV.flat().map(x => ({ label: x.labels[idx], type: 'Navigate', href: x.href }))
  const source = query ? [...navItems, ...entities] : recent.length > 0 ? recent : navItems
  const results = source.filter(x => !query || `${x.label} ${x.type}`.toLowerCase().includes(query.toLowerCase())).slice(0, 12)
  const groups = query ? [...new Set(results.map(item => item.type))].map(type => ({ label: type, items: results.map((item, index) => ({ item, index })).filter(entry => entry.item.type === type) })) : [{ label: recent.length > 0 ? p.recent : 'Navigate', items: results.map((item, index) => ({ item, index })) }]
  const openItem = (index: number) => { const item = results[index]; if (!item) return; setRecent(rememberItem(item)); navigate(item.href); close() }
  return <div className="dialog-layer" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget) close() }}><section ref={dialogRef} onKeyDown={onDialogKeyDown} className="command-dialog" role="dialog" aria-modal="true" aria-label={p.palette}><div className="command-dialog__input"><Icon name="search" /><input autoFocus value={query} onChange={e => { setQuery(e.target.value); setActiveIndex(0) }} onKeyDown={e => { if (e.key === 'ArrowDown') { e.preventDefault(); setActiveIndex(current => results.length ? (current + 1) % results.length : 0) } else if (e.key === 'ArrowUp') { e.preventDefault(); setActiveIndex(current => results.length ? (current - 1 + results.length) % results.length : 0) } else if (e.key === 'Enter') { e.preventDefault(); openItem(activeIndex) } }} placeholder={copy[locale].search} /></div><div className="command-results">{groups.map(group => <section className="command-group" key={group.label}><p className="command-group__label">{group.label}</p>{group.items.map(({ item, index }) => <button key={`${item.type}-${item.href}-${index}`} data-active={index === activeIndex || undefined} onMouseEnter={() => setActiveIndex(index)} onClick={() => openItem(index)}><span>{item.label}</span><small>{item.type}</small></button>)}</section>)}{results.length === 0 && <p>{copy[locale].noResults}</p>}</div><footer><span><kbd>↑↓</kbd> {p.browse}</span><span><kbd>↵</kbd> {p.open}</span><span><kbd>C</kbd> {p.newIssue}</span><span><kbd>esc</kbd> {p.close}</span></footer></section></div>
}

function CreateIssueDialog({ workspaceId, close, labels, navigate, returnFocus }: { workspaceId: string; close: () => void; labels: typeof copy[keyof typeof copy]; navigate: (href: string) => void; returnFocus?: HTMLElement | null }) {
  const create = useCreateIssue(apiClient, workspaceId)
  const [title, setTitle] = useState(''); const [description, setDescription] = useState('')
  const dialogRef = useRef<HTMLFormElement>(null)
  const onDialogKeyDown = useDialogFocus(dialogRef, returnFocus)
  return <div className="dialog-layer" onMouseDown={e => { if (e.target === e.currentTarget) close() }}><form ref={dialogRef} onKeyDown={onDialogKeyDown} className="create-dialog" role="dialog" aria-modal="true" onSubmit={e => { e.preventDefault(); if (!title.trim()) return; create.mutate({ title: title.trim(), description }, { onSuccess: issue => { close(); navigate(`/issues/${issue.id}`) } }) }}><header><div><span className="dialog-eyebrow">ISSUE / NEW</span><h2>{labels.newIssue}</h2></div><button type="button" className="icon-button" aria-label="Close" onClick={close}><Icon name="close" /></button></header><label>{labels.title}<Input autoFocus value={title} onChange={e => setTitle(e.target.value)} /></label><label>{labels.description}<Textarea value={description} onChange={e => setDescription(e.target.value)} placeholder={labels.description} /></label>{create.isError && <p className="form-error">Could not create the issue. Your draft is still here; check the local API and try again.</p>}<footer><Button type="button" variant="ghost" onClick={close}>{labels.cancel}</Button><Button type="submit" disabled={!title.trim() || create.isPending}>{create.isPending ? labels.creating : labels.create}</Button></footer></form></div>
}

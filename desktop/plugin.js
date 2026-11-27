import * as sdk from '@hermes/plugin-sdk'
import React, { useEffect, useMemo, useRef, useState } from 'react'

const h = React.createElement
const { Button, ROUTES_AREA, host } = sdk
const SIDEBAR_NAV_AREA = 'sidebar.nav'

function createRequestEpoch() {
  let current = 0
  return {
    next() {
      current += 1
      return current
    },
    isCurrent(value) {
      return value === current
    },
  }
}

function parseCommandReply(reply) {
  let output = reply?.output ?? reply?.result?.output ?? reply
  if (typeof output !== 'string') return output
  if (output.startsWith('Plugin command error:')) throw new Error(output)
  try {
    return JSON.parse(output)
  } catch {
    return output
  }
}

function memoryAssetItems(value) {
  const candidates = [value, value?.result, value?.output, value?.result?.output]
  for (const candidate of candidates) {
    let parsed = candidate
    if (typeof parsed === 'string') {
      try {
        parsed = JSON.parse(parsed)
      } catch {
        continue
      }
    }
    if (Array.isArray(parsed?.assets)) return parsed.assets
  }
  return []
}

const PANEL_STYLE = {
  padding: 24,
  maxWidth: 820,
  margin: '0 auto',
  color: '#1f2937',
  lineHeight: 1.45,
}

const FIELD_STYLE = {
  margin: '0 0 18px',
}

const SELECT_STYLE = {
  display: 'block',
  width: '100%',
  minHeight: 44,
  padding: '9px 38px 9px 12px',
  fontSize: 15,
  lineHeight: 1.4,
  color: '#111827',
  backgroundColor: '#fff',
  border: '1px solid #9ca3af',
  borderRadius: 8,
  boxSizing: 'border-box',
}

const SECTION_STYLE = {
  margin: '20px 0',
  padding: '18px 18px 2px',
  border: '1px solid #d1d5db',
  borderRadius: 10,
  backgroundColor: '#f9fafb',
}

function SelectField({ label, hint, value, onChange, disabled, children }) {
  return h('div', { style: FIELD_STYLE },
    h('label', { style: { display: 'block', fontWeight: 600, fontSize: 14, marginBottom: 6 } }, label),
    h('select', {
      value,
      disabled,
      onChange,
      'aria-label': label,
      style: {
        ...SELECT_STYLE,
        color: value ? '#111827' : '#6b7280',
        backgroundColor: disabled ? '#f3f4f6' : '#fff',
      },
    }, children),
    hint && h('div', { style: { marginTop: 5, fontSize: 12, color: '#6b7280' } }, hint),
  )
}

const sessionParams = { include_hidden: true, limit: 200 }

function matchingSession(sessions, roomId) {
  return sessions.find(item => item?.source === 'bot_room' && (
    item?.room_id === roomId ||
    item?.chat_id === roomId ||
    item?.title === `Group: ${roomId}` ||
    item?.root_title === `Group: ${roomId}` ||
    item?.title?.includes(roomId)
  ))
}

async function locateGroupSession(roomId) {
  const diagnostics = []
  const lookupParams = { ...sessionParams, title: `Group: ${roomId}` }
  const active = await host.request('session.list', lookupParams)
  const activeSessions = active?.sessions ?? active?.items ?? []
  const activeMatch = matchingSession(activeSessions, roomId)
  diagnostics.push(`profile=${host.state?.profile?.get?.() ?? '<unknown>'};session_total=${activeSessions.length};bot_room=${activeSessions.filter(item => item?.source === 'bot_room').length}`)
  if (activeMatch) return { route: null, session: activeMatch, diagnostics }

  if (typeof host.profileRoutes === 'function' && typeof host.requestProfile === 'function') {
    let routes = []
    try {
      routes = await host.profileRoutes()
    } catch (error) {
      diagnostics.push(`profile_routes_error=${String(error)}`)
    }
    for (const route of routes) {
      try {
        const listing = await host.requestProfile(route, 'session.list', lookupParams, 60000)
        const sessions = listing?.sessions ?? listing?.items ?? []
        const label = route?.targetProfile ?? route?.profile ?? '<unknown>'
        diagnostics.push(`route=${label};session_total=${sessions.length};bot_room=${sessions.filter(item => item?.source === 'bot_room').length}`)
        const match = matchingSession(sessions, roomId)
        if (match) return { route, session: match, diagnostics }
      } catch (error) {
        diagnostics.push(`route=${route?.targetProfile ?? route?.profile ?? '<unknown>'};error=${String(error)}`)
      }
    }
  }
  return { route: null, session: null, diagnostics }
}

async function groupCommand(roomId, payload) {
  const located = await locateGroupSession(roomId)
  const session = located.session
  if (!session?.session_id) {
    const diagnostic = located.diagnostics.join(' | ')
    console.warn('[hermes-group-project-binding] session lookup failed', { roomId, diagnostic })
    throw new Error(`当前群没有可用的 Hermes 会话（${diagnostic}）`)
  }
  const command = `/group-binding ${JSON.stringify({ room_id: roomId, ...payload })}`
  const reply = located.route
    ? await host.requestProfile(located.route, 'slash.exec', { session_id: session.session_id, command }, 60000)
    : await host.request('slash.exec', { session_id: session.session_id, command }, 60000)
  return parseCommandReply(reply)
}

async function rooms() {
  const result = await host.request('groups.list', { limit: 100 })
  return result?.rooms ?? result?.items ?? []
}

function RoleClaimPanel() {
  const [roomRows, setRoomRows] = useState([])
  const [roomId, setRoomId] = useState('')
  const [catalog, setCatalog] = useState(null)
  const [teamId, setTeamId] = useState('')
  const [taskId, setTaskId] = useState('')
  const [options, setOptions] = useState([])
  const [selections, setSelections] = useState({})
  const [memoryAssets, setMemoryAssets] = useState([])
  const [sharedMemoryId, setSharedMemoryId] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const catalogEpoch = useRef(createRequestEpoch())
  const roleEpoch = useRef(createRequestEpoch())
  const memoryEpoch = useRef(createRequestEpoch())

  useEffect(() => {
    rooms().then(rows => {
      setRoomRows(rows)
      if (!roomId && rows[0]?.room_id) void loadCatalog(rows[0].room_id)
    }).catch(error => setMessage(String(error)))
  }, [])

  const teams = catalog?.catalog ?? []
  const selectedTeam = useMemo(() => teams.find(item => item?.team?.team_id === teamId), [teams, teamId])
  const tasks = selectedTeam?.tasks ?? []
  const members = catalog?.room?.members ?? []

  const loadCatalog = async id => {
    const requestId = catalogEpoch.current.next()
    memoryEpoch.current.next()
    setRoomId(id)
    setCatalog(null)
    setOptions([])
    setSelections({})
    setMemoryAssets([])
    setSharedMemoryId('')
    setMessage('正在读取群成员、Team 和项目…')
    try {
      const value = await groupCommand(id, { action: 'catalog' })
      if (!catalogEpoch.current.isCurrent(requestId)) return
      setCatalog(value)
      const initialTeamId = value.catalog?.[0]?.team?.team_id ?? ''
      setTeamId(initialTeamId)
      setTaskId('')
      setMessage(initialTeamId ? '请选择 Team 和项目' : '请选择 Team 和项目')
    } catch (error) {
      if (!catalogEpoch.current.isCurrent(requestId)) return
      setMessage(String(error))
    }
  }

  const loadRoles = async id => {
    const requestId = roleEpoch.current.next()
    setTaskId(id)
    setOptions([])
    setSelections({})
    if (!id) return
    setMessage('正在读取当前项目的角色目录…')
    try {
      const value = await groupCommand(roomId, { action: 'roles', task_id: id })
      if (!roleEpoch.current.isCurrent(requestId)) return
      setOptions(value.options ?? [])
      const next = {}
      for (const member of value.room?.members ?? members) {
        const recommended = value.options?.find(option => option.task_agent?.role_in_task === member.handle)
        if (recommended?.task_agent?.id) next[member.member_id] = { task_link_id: recommended.task_agent.id }
      }
      setSelections(next)
      setMessage('请检查每个 profile 的角色，然后提交确认')
    } catch (error) {
      if (!roleEpoch.current.isCurrent(requestId)) return
      setMessage(String(error))
    }
  }

  const loadMemoryAssets = async id => {
    const requestId = memoryEpoch.current.next()
    setMemoryAssets([])
    setSharedMemoryId('')
    if (!id) return
    try {
      const value = await groupCommand(roomId, { action: 'memory-assets', team_id: id })
      if (!memoryEpoch.current.isCurrent(requestId)) return
      const assets = memoryAssetItems(value)
      setMemoryAssets(assets)
      setMessage(`已加载 ${assets.length} 个共享 Chat Memory`)
    } catch (error) {
      if (!memoryEpoch.current.isCurrent(requestId)) return
      setMessage(String(error))
    }
  }

  useEffect(() => {
    if (roomId && teamId) void loadMemoryAssets(teamId)
  }, [roomId, teamId])

  const submit = async () => {
    setBusy(true)
    setMessage('正在重新校验提案并等待房主确认…')
    try {
      const prepared = await groupCommand(roomId, {
        action: 'prepare', team_id: teamId, task_id: taskId, selections,
        shared_memory_id: sharedMemoryId || undefined,
      })
      const result = await groupCommand(roomId, { action: 'submit', proposal: prepared.proposal })
      setMessage(String(result))
    } catch (error) {
      setMessage(String(error))
    } finally {
      setBusy(false)
    }
  }

  return h('div', { style: PANEL_STYLE },
    h('h1', null, 'Hermes 项目绑定'),
    h('p', { style: { margin: '0 0 18px', color: '#4b5563' } }, '只绑定当前 Canonical 群。提交后由房主确认一次，后续普通群聊无需重复选择。'),
    h('div', { style: SECTION_STYLE },
      h('h2', { style: { margin: '0 0 14px', fontSize: 17 } }, '1. 选择群'),
      h(SelectField, {
        label: '群',
        value: roomId,
        onChange: event => loadCatalog(event.target.value),
        hint: roomId ? `当前群 ID：${roomId}` : '请选择要绑定的 Canonical 群',
      },
        h('option', { value: '' }, '请选择群'),
        roomRows.map(room => h('option', { key: room.room_id, value: room.room_id }, room.name || room.room_id))
      ),
    ),
    catalog && h(React.Fragment, null,
      h('div', { style: SECTION_STYLE },
        h('h2', { style: { margin: '0 0 14px', fontSize: 17 } }, '2. 选择 Tencent 范围'),
        h(SelectField, {
          label: 'Team',
          value: teamId,
          onChange: event => { setTeamId(event.target.value); setTaskId(''); setOptions([]) },
          hint: teamId ? `Team ID：${teamId}` : '先选择 Team，下面才会加载项目和共享记忆',
        },
          h('option', { value: '' }, '请选择 Team'),
          teams.map(item => h('option', { key: item.team.team_id, value: item.team.team_id }, `${item.team.name || '未命名 Team'} · ${item.team.team_id}`))
        ),
        h(SelectField, {
          label: `共享 Chat Memory（${memoryAssets.length} 个可用）`,
          value: sharedMemoryId,
          disabled: !teamId,
          onChange: event => setSharedMemoryId(event.target.value),
          hint: sharedMemoryId ? '已选择团队共享记忆；不选择则只使用各角色私有记忆' : '默认只使用各角色私有记忆',
        },
          h('option', { value: '' }, '仅使用各角色私有记忆'),
          memoryAssets.map(asset => h('option', { key: asset.id, value: asset.id }, `${asset.title || '未命名记忆'} · ${asset.id}`))
        ),
        h(SelectField, {
          label: '项目 Task',
          value: taskId,
          disabled: !teamId,
          onChange: event => loadRoles(event.target.value),
          hint: taskId ? `Task ID：${taskId}` : '请选择这个 Team 下要绑定的项目 Task',
        },
          h('option', { value: '' }, teamId ? '请选择项目 Task' : '请先选择 Team'),
          tasks.map(task => h('option', { key: task.task_id, value: task.task_id }, `${task.title || '未命名项目'} · ${task.task_id}`))
        ),
      ),
      options.length > 0 && h('div', null,
        h('div', { style: SECTION_STYLE },
          h('h2', { style: { margin: '0 0 6px', fontSize: 17 } }, '3. 角色认领'),
          h('p', { style: { margin: '0 0 14px', fontSize: 13, color: '#4b5563' } }, '每个 profile 必须选择一个 TaskAgent 角色；确认后会锁定到当前项目。'),
        members.map(member => h('label', { key: member.member_id, style: { display: 'block', margin: '12px 0' } },
          h('span', { style: { display: 'block', fontWeight: 600, fontSize: 14, marginBottom: 6 } }, `${member.profile} · @${member.handle}`),
          h('select', { value: selections[member.member_id]?.task_link_id ?? '', onChange: event => setSelections({ ...selections, [member.member_id]: { task_link_id: event.target.value } }), 'aria-label': `${member.profile} 的角色`, style: { ...SELECT_STYLE, minHeight: 42 } },
            h('option', { value: '' }, '选择角色'),
            options.map(option => h('option', { key: option.task_agent.id, value: option.task_agent.id }, `${option.task_agent.role_in_task || '未命名角色'} · ${option.agent.name || option.agent.agent_id}`))
          )
        )),
        h(Button, { disabled: busy || !taskId || members.some(member => !selections[member.member_id]?.task_link_id), onClick: submit }, busy ? '处理中…' : '提交并请求房主确认')
        ),
      )
    ),
    message && h('div', { role: 'status', style: { marginTop: 20, padding: '12px 14px', borderRadius: 8, backgroundColor: message.startsWith('插件命令出错') || message.startsWith('Error') ? '#fef2f2' : '#eff6ff', color: '#1f2937', whiteSpace: 'pre-wrap' } }, message)
  )
}

export default {
  id: 'hermes-group-project-binding',
  name: 'Hermes Group Project Binding',
  description: '在 WebApp 中选择 Canonical 群的 Team、项目和角色',
  defaultEnabled: false,
  register(ctx) {
    const dispose = ctx.register({
      id: 'role-claim',
      area: ROUTES_AREA,
      title: 'Hermes 项目绑定',
      data: { path: '/group-project-binding' },
      render: () => h(RoleClaimPanel)
    })
    let disposeNav = () => {}
    try {
      disposeNav = ctx.register({
        id: 'role-claim-nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/group-project-binding', label: 'Hermes 项目绑定', codicon: 'project' }
      })
    } catch (error) {
      console.warn('[hermes-group-project-binding] sidebar navigation unavailable', error)
    }
    ctx.onDispose(() => {
      dispose()
      disposeNav()
    })
  }
}


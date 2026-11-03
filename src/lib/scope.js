/**
 * hermes-console · 作用域（agent）状态：枚举、默认值、持久化、失败退化。
 *
 * 三个必须扛住的情况：
 *   1. `/agents` 慢 → 选择器先显示「当前 profile」（后端不传 agent 时就是这个作用域），
 *      不阻塞外壳渲染；
 *   2. `/agents` 失败或结构不对 → 退化成一个选项 + 一行原因，**绝不白屏**，
 *      导航、分区、刷新按钮照常可用；
 *   3. 上次选的 agent 已经不存在（profile 被删）→ 自动回落到后端给的 current 并改写偏好。
 *
 * 持久化 key：`scope.agent`（宿主会命名空间化成
 * `hermes.plugin.hermes-console.scope.agent`）。读 = 首次 render 之前同步读一次；
 * 写 = 用户在选择器上改动时立刻写，以及「发现存的 id 不合法」时自动改写。
 */
import { useEffect, useState } from 'react'

import { useQuery } from './query.js'
import { get } from './rest.js'
import { readPref, writePref } from './storage.js'

/** 作用域偏好键。 */
export var SCOPE_KEY = 'scope.agent'

function fetchAgents() {
  return get('/agents')
}

/**
 * agent 枚举 + 当前作用域。
 * @returns {{
 *   status: 'loading'|'ok'|'error',
 *   reason: string,
 *   options: Array<{id: string, label: string}>,
 *   value: string,
 *   busy: boolean,
 *   setValue: (id: string) => void,
 *   reload: Function
 * }}
 */
export function useScope() {
  var query = useQuery('agents', fetchAgents, { staleMs: 60000 })
  var state = useState(readPref(SCOPE_KEY, ''))
  var stored = state[0]
  var setStored = state[1]

  var payload = query.data
  var agents = []
  var status = query.status
  var reason = ''

  if (query.status === 'ok') {
    if (payload && Array.isArray(payload.agents)) {
      agents = payload.agents.filter(function (item) {
        return item && typeof item.id === 'string' && item.id
      })
    } else {
      status = 'error'
      reason = '后端 /agents 没有返回 agents 数组（可能是旧版本后端），只能先用当前作用域。'
    }
  } else if (query.status === 'error') {
    reason = 'agent 列表没读到：' + query.error + ' —— 已退回仅当前作用域，可点「刷新」重试。'
  }

  var ids = agents.map(function (item) {
    return item.id
  })
  var backendCurrent = payload && typeof payload.current === 'string' ? payload.current : ''
  var value = ''
  if (stored && (!ids.length || ids.indexOf(stored) !== -1)) value = stored
  else if (backendCurrent) value = backendCurrent
  else if (ids.length) value = ids[0]
  else value = stored || ''

  // 首次打开 / 上次选的 agent 已消失：把纠正结果落回偏好，下次不再重复纠正
  useEffect(
    function () {
      if (value && value !== stored) {
        writePref(SCOPE_KEY, value)
        setStored(value)
      }
    },
    [value, stored]
  )

  var options = agents.length
    ? agents.map(function (item) {
        return {
          id: item.id,
          label: item.label && item.label !== item.id ? item.label + '（' + item.id + '）' : item.id
        }
      })
    : [
        {
          id: value,
          label: value ? value + '（当前）' : '当前 profile'
        }
      ]

  return {
    status: status,
    reason: reason,
    options: options,
    value: value,
    busy: query.busy,
    setValue: function (next) {
      writePref(SCOPE_KEY, next)
      setStored(next)
    },
    reload: query.reload
  }
}

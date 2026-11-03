/**
 * hermes-console · 项目分组 AI 的 **provider/model 两级配置**面板（t_3c2817b1）。
 *
 * 症状③「无法设置 AI 的 provider/model」的三层成因，这里逐条堵：
 *   1. **没有界面**：旧面板只显示「模型 xxx」一个 chip，没有任何可编辑控件 ——本文件就是那个界面；
 *   2. **没按 Agent 独立**：后端 `autogroup_model` 已改成 `{"agents":{...},"global":{...}}`，
 *      PUT 带 `agent=` 只动那一个 Agent 的声明（见 api_autogroup.put_ai_model）；
 *   3. **两级联动没做**：provider 变了旧 model 必须清空/重校验 ——这里 provider 的 onChange
 *      一律先 `setModel('')` 再按新 provider 重新拉模型清单，保存时后端还会再校验一次。
 *
 * 路由（全部是现存路由，无 404）：
 *   GET  /autogroup/ai/model?agent=X          → config / resolved / declared_agents / providers / models / recent_calls
 *   GET  /autogroup/ai/model/options?agent=X  → 该 agent 的 provider 目录（密钥只有 key_env + key_present）
 *   GET  /autogroup/ai/model/options?agent=X&provider=Y → 该 provider 的模型清单（source=config|remote|none）
 *   PUT  /autogroup/ai/model {agent, mode, provider, model} → 写 + **回读盘上真值**
 *
 * 四条硬约束：
 *   * 保存后只认**响应里的回读值**（`data.agent_declaration` / `data.resolved`），不显示本地 state；
 *   * 密钥只显示变量名 + 是否存在，取值绝不回显；
 *   * provider 变了必须清空旧 model（清空后未选 model 时「保存」按钮禁用）；
 *   * 来源层四种（该 Agent 独立 / 该 Agent 跟随全局 / 跟随 global 段 / 模块默认）都写明。
 */
import { jsx as jsxRuntime, jsxs as jsxsRuntime } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { num } from '../lib/format.js'
import { get, send } from '../lib/rest.js'
import { ErrorState } from '../lib/states.js'

function splitKey(props) {
  if (!props || props.key === undefined) return [props, undefined]
  var next = {}
  Object.keys(props).forEach(function (name) {
    if (name !== 'key') next[name] = props[name]
  })
  return [next, props.key]
}

function jsx(type, props, key) {
  var pair = splitKey(props)
  return jsxRuntime(type, pair[0], key !== undefined ? key : pair[1])
}

function jsxs(type, props, key) {
  var pair = splitKey(props)
  return jsxsRuntime(type, pair[0], key !== undefined ? key : pair[1])
}

/** 「global 段」的伪 agent id（UI 里可选的一项，不是真实 profile）。 */
export var GLOBAL_TARGET = '__global__'

var LAYER_TEXT = {
  agent: '该 Agent 独立',
  agent_global: '该 Agent 跟随全局（config.yaml 当前模型）',
  global: '跟随 global 段',
  module_default: '模块默认（无任何声明，回落该 Agent config.yaml）'
}

function unwrap(payload) {
  if (!payload) return {}
  if (payload.data && typeof payload.data === 'object') return payload.data
  return payload
}

function errText(res, fallback) {
  if (!res) return fallback
  if (res.message) return res.message
  if (res.error && res.error.message) return res.error.message
  return fallback
}

function chip(text, key, title) {
  return jsx('span', { key: key, className: 'hc-chip', title: title || '', children: text })
}

/** 一行键值（键在左、值在右），值过长会自动换行。 */
function kv(label, value, key, dataName, extra) {
  return jsxs('div', {
    key: key,
    className: 'hc-agg__model-kv',
    'data-agm-kv': dataName || '',
    children: [
      jsx('span', { key: 'k', className: 'hc-agg__model-k', children: label }),
      jsx('span', { key: 'v', className: 'hc-agg__model-v', children: value })
    ]
  })
}

/**
 * provider/model 配置面板。
 *
 * props:
 *   agents    —— [{id, label, is_default}]（外层已从 /agents 拿到，这里只渲染）
 *   onSaved   —— 保存成功回调（外层用来刷新候选总数等）
 */
/**
 * 打开面板时的「配置对象」初值（t_11b7edbb）。
 *
 * **不许再停在 global 段**：global 段既不能写 provider/model（后端 400 要求点名 Agent），
 * mode=global 又是空写入，所以面板把用户直接送到一个真实 Agent 上：
 *   1. 外层传了「当前 Agent」（``props.currentAgent`` / ``props.agent``）就用它；
 *   2. 否则回落到 agents 列表里的默认 Agent（``is_default``），再不行取第一个；
 *   3. 一个 Agent 都取不到 ⇒ 返回 ``''``，界面显式提示「请先选择 Agent」。
 * 返回 ``auto=true`` 表示是面板自己挑的（界面要写明，别让用户以为外层选中了它）。
 */
export function resolveInitialTarget(agents, current) {
  var wanted = String(current || '')
  if (wanted && wanted !== GLOBAL_TARGET && wanted !== '__all__') return { target: wanted, auto: false }
  var list = agents || []
  var i
  for (i = 0; i < list.length; i += 1) {
    if (list[i] && list[i].is_default && list[i].id) return { target: list[i].id, auto: true }
  }
  for (i = 0; i < list.length; i += 1) {
    if (list[i] && list[i].id) return { target: list[i].id, auto: true }
  }
  return { target: '', auto: false }
}

export function AutogroupModelPanel(props) {
  var agents = props.agents || []
  var initial = resolveInitialTarget(agents, props.currentAgent || props.agent)
  var targetState = useState(initial.target)
  var target = targetState[0]
  var setTarget = targetState[1]
  var autoState = useState(initial.auto)
  var autoPicked = autoState[0]
  var setAutoPicked = autoState[1]

  var dataState = useState({ phase: 'loading', error: '', data: null })
  var dataView = dataState[0]
  var setData = dataState[1]

  var optionsState = useState({ phase: 'idle', error: '', providers: [], models: [], source: null })
  var optionsView = optionsState[0]
  var setOptions = optionsState[1]

  var formState = useState({ mode: 'global', provider: '', model: '', dirty: false })
  var form = formState[0]
  var setForm = formState[1]

  var saveState = useState({ phase: 'idle', text: '', error: '', readback: null })
  var saveView = saveState[0]
  var setSave = saveState[1]

  var agentQuery = target === GLOBAL_TARGET ? '' : target

  /** 读配置（agent 切换时重读：因为「来源层 / 实际生效值」是按 agent 的）。 */
    function loadConfig(agentId) {
      var params = {}
      if (agentId && agentId !== GLOBAL_TARGET) params.agent = agentId
      if (!agentId) {
        // 还没选 Agent（agents 列表没到位）：不请求、不留「读取中」，界面给「请先选择 Agent」。
        setData({ phase: 'idle', error: '', data: null })
        return
      }
      setData({ phase: 'loading', error: '', data: null })
    get('/autogroup/ai/model', { params: params }).then(function (res) {
      if (!res || res.ok === false) {
        setData({ phase: 'error', error: errText(res, '读取模型配置失败'), data: null })
        return
      }
      // get() 返回 `{ok, data: <响应体>}`，响应体自身还是 `{ok, data:{...}}`：
      // 必须 unwrap(res.data)，多套一层会让 resolved / providers 全丢（面板永远「读取中」）。
      var data = unwrap(res.data)
      setData({ phase: 'ready', error: '', data: data })
      // 表单初值 = **盘上该 Agent 的声明**（不是实际生效值：生效值可能来自 config.yaml）
      var decl = data.agent_declaration || {}
      var group = decl.group || {}
      var isGlobal = agentId === GLOBAL_TARGET
      var globDecl = data.global_declaration || {}
      setForm({
        mode: isGlobal ? (globDecl.mode || 'global') : (group.declared ? (group.mode || 'global') : 'inherit'),
        provider: (isGlobal ? globDecl.provider : group.provider) || '',
        model: (isGlobal ? globDecl.model : group.model) || '',
        dirty: false
      })
      setSave({ phase: 'idle', text: '', error: '', readback: null })
    })
  }

  /** 读 provider 目录（及当前 provider 的模型清单）。 */
    function loadOptions(agentId, provider) {
      if (!agentId || agentId === GLOBAL_TARGET) {
        // global 段没有自己的 provider 目录：provider/model 是按 Agent 的（凭据与 config.yaml 都在该 Agent 名下）。
        // 以前这里拿 default 的清单冒充 global 的，就是「9 个看起来能点的行」的来源。
        setOptions({ phase: 'idle', error: '', providers: [], models: [], source: null })
        return
      }
      var agent = agentId
    setOptions(function (current) {
      return { phase: 'loading', error: '', providers: current.providers, models: current.models,
               source: current.source }
    })
    get('/autogroup/ai/model/options', { params: { agent: agent } }).then(function (res) {
      if (!res || res.ok === false) {
        setOptions({ phase: 'error', error: errText(res, '读取 provider 目录失败'),
                     providers: [], models: [], source: null })
        return
      }
      var data = unwrap(res.data)
      var providers = data.providers || []
      if (!provider) {
        setOptions({ phase: 'ready', error: '', providers: providers, models: [], source: null })
        return
      }
      get('/autogroup/ai/model/options', { params: { agent: agent, provider: provider } }).then(function (res2) {
        if (!res2 || res2.ok === false) {
          setOptions({ phase: 'ready', error: errText(res2, '读取模型清单失败'),
                       providers: providers, models: [], source: null })
          return
        }
        var listing = unwrap(res2.data)
        setOptions({
          phase: 'ready',
          error: '',
          providers: providers,
          models: listing.models || [],
          source: listing.source || null
        })
      })
    })
  }

  useEffect(
    function () {
      loadConfig(target)
    },
    [target]
  )

  useEffect(
    function () {
      loadOptions(target, form.provider)
      // 只在 agent / provider 变化时重拉；form.model 变化不该触发网络请求
    },
    [target, form.provider]
  )

  // isGlobal 必须在下面「global 段自己的声明」分支之前算出来：
  // `var` 提升只提升声明不提升赋值，放在后面会让 global 分支永远拿到 undefined。
  var isGlobal = target === GLOBAL_TARGET
  var data = dataView.data || {}
  var resolved = data.resolved || {}
  var globDecl = data.global_declaration || {}
  if (isGlobal) {
    // 选「global 段」时，看的是这一层自己的声明（后端 describe 的 resolved 是按具体 Agent 算的，
    // 拿来显示 global 段会误导：它可能来自某个 agent 的 config.yaml）。
    resolved = {
      layer: globDecl.declared ? 'global' : 'module_default',
      layer_label: globDecl.declared
        ? '跟随 global 段'
        : '模块默认（global 段也没声明，回落各 Agent 自己的 config.yaml）',
      provider: globDecl.provider || null,
      model: globDecl.model || null,
      mode: globDecl.mode || 'global',
      reason: globDecl.declared
        ? 'global 段声明：mode=' + (globDecl.mode || 'global') +
          (globDecl.provider ? '（' + globDecl.provider + '/' + globDecl.model + '）' : '（跟随各 Agent config.yaml）')
        : 'global 段没有声明：每个 Agent 各自回落它自己 config.yaml 的当前模型',
      config_exists: (data.config || {}).exists,
      config_legacy: (data.config || {}).legacy
    }
  }
  var decl = data.agent_declaration || {}
    var declaredAgents = data.declared_agents || {}
    var providers = optionsView.providers || []
    var models = optionsView.models || []
    var noTarget = !target
    var inheritMode = form.mode === 'inherit'
    // t_11b7edbb：global 段 + mode=global 是**空写入**（盘上什么都不变）⇒ 保存必须禁用，
    // 不许再让「保存成功」的提示落在无变化的结果上。global 段的另一条路 mode=module 被后端 400
    // 拒绝（provider/model 必须按点名 Agent 的凭据校验）⇒ global 段整体只读，去选 Agent。
    var canSave = !isGlobal && !noTarget && !inheritMode && (
      form.mode === 'global' ? true : (form.provider !== '' && form.model !== '')
    )

  /** 换配置对象：清空上一个对象的表单残留（provider/model 属于上一个 Agent）。 */
  function pickTarget(next) {
    setAutoPicked(false)
    setForm({ mode: 'global', provider: '', model: '', dirty: false })
    setSave({ phase: 'idle', text: '', error: '', readback: null })
    setTarget(next)
  }

  /** provider 变了：**先清空旧 model**（旧模型属于旧 provider，留着就是错配）。 */
  function onProviderChange(next) {
    if (next === form.provider) return
    setForm({ mode: form.mode, provider: next, model: '', dirty: true })
    setSave({ phase: 'idle', text: '', error: '', readback: null })
  }

  function onModelChange(next) {
    setForm({ mode: form.mode, provider: form.provider, model: next, dirty: true })
    setSave({ phase: 'idle', text: '', error: '', readback: null })
  }

  function onModeChange(next) {
    setForm({ mode: next, provider: next === 'module' ? form.provider : '',
              model: next === 'module' ? form.model : '', dirty: true })
    setSave({ phase: 'idle', text: '', error: '', readback: null })
  }

  /** 点 provider 行 = 用这个 provider（t_11b7edbb C：provider 行是入口，不是装饰）。 */
    function pickProvider(name) {
      if (isGlobal || noTarget || !name) return
      if (name === form.provider && form.mode === 'module') return
      setForm({ mode: 'module', provider: name, model: '', dirty: true })
      setSave({ phase: 'idle', text: '', error: '', readback: null })
    }

    function save() {
      if (isGlobal) {
        // 防御：按钮已是禁用态，这里只兜住程序化点击 —— 明确说「此处不写入」，绝不报成功。
        setSave({ phase: 'blocked', readback: null, error: '',
                  text: 'global 段不写入：它没有自己的 provider/model（各 Agent 跟随各自 config.yaml）。' +
                    '要指定具体模型，请在上方「配置对象」里选一个 Agent。' })
        return
      }
      if (!canSave) return
    var body = { mode: form.mode }
    if (!isGlobal) body.agent = target
    if (form.mode === 'module') {
      body.provider = form.provider
      body.model = form.model
    }
    setSave({ phase: 'busy', text: '', error: '', readback: null })
    send('/autogroup/ai/model', body, { method: 'PUT' }).then(function (res) {
      if (!res || res.ok === false) {
        setSave({ phase: 'error', text: '', error: errText(res, '保存失败'), readback: null })
        return
      }
      // **只认响应里的回读值**：后端 save 之后重新 load 了盘上文件
      var payload = unwrap(res.data)
      var saved = payload.saved || {}
      setSave({
        phase: 'done',
        text: '已保存并回读盘上真值：' +
          (saved.target || '') + ' · mode=' + (saved.mode || '—') +
          (saved.provider ? ' · provider=' + saved.provider + ' · model=' + saved.model : ''),
        error: '',
        readback: { resolved: payload.resolved || {}, decl: payload.agent_declaration || {} }
      })
      setData({ phase: 'ready', error: '', data: payload })
      var nextDecl = payload.agent_declaration || {}
      var nextGroup = nextDecl.group || {}
      setForm({
        mode: nextGroup.declared ? (nextGroup.mode || 'global') : 'inherit',
        provider: nextGroup.provider || '',
        model: nextGroup.model || '',
        dirty: false
      })
      if (typeof props.onSaved === 'function') props.onSaved()
    })
  }

  function providerRow(name) {
    var found = null
    providers.forEach(function (row) {
      if (row.provider === name) found = row
    })
    return found
  }

  function renderProviders() {
    if (optionsView.phase === 'loading' && !providers.length) {
      return jsx('div', { className: 'hc-agg__hint', children: '正在读该 Agent 的 provider 目录…' })
    }
    if (optionsView.phase === 'error') {
      return jsx('div', { className: 'hc-agg__model-err', 'data-agm-providers-error': '1',
                          children: optionsView.error })
    }
    if (!providers.length) {
      return jsx('div', {
        className: 'hc-agg__model-err',
        'data-agm-providers-empty': '1',
        children: '该 Agent 的 config.yaml 里没有声明任何 provider，也没有内置 provider 的密钥 —— ' +
          '先到「模型」分区给这个 Agent 配一个 provider 与密钥，这里才有可选项。'
      })
    }
    return jsxs('div', {
      className: 'hc-agg__model-providers',
      'data-agm-providers': String(providers.length),
      children: [
        jsx('div', { key: 'h', className: 'hc-agg__model-providers-head',
                             children: '该 Agent 可用的 provider（点一行即选用；密钥只显示变量名与是否存在）' }),
        jsx('div', {
          key: 'rows',
          className: 'hc-agg__model-providers-rows',
          children: providers.map(function (row) {
                      var active = !isGlobal && row.provider === form.provider
                      return jsxs('button', {
                        key: row.provider,
                        type: 'button',
                        className: 'hc-agg__model-provider hc-agg__model-provider--pick' +
                          (active ? ' hc-agg__model-provider--on' : ''),
                        'data-agm-provider': row.provider,
                        'data-agm-provider-pick': '1',
                        'data-agm-provider-key-present': row.key_present ? '1' : '0',
                        'aria-pressed': active ? 'true' : 'false',
                        title: '点这一行 = 用这个 provider（切到「独立模型」并清空旧 model）',
                        onClick: function () { pickProvider(row.provider) },
                        children: [
                jsx('span', { key: 'n', className: 'hc-agg__model-provider-name', children: row.label || row.provider }),
                jsx('span', { key: 'p', className: 'hc-agg__model-provider-k',
                              children: row.provider }),
                jsx('span', {
                  key: 'k',
                  className: 'hc-chip',
                  title: '只显示环境变量名与是否存在，取值绝不回显',
                  children: (row.key_env || (row.key_envs || [])[0] || '（无 key_env）') +
                    (row.key_present ? ' · 已设置' : ' · 未设置')
                }),
                row.current ? jsx('span', { key: 'c', className: 'hc-chip hc-chip--on',
                                            children: 'config.yaml 当前使用' }) : null
              ]
            }, row.provider)
          })
        })
      ]
    })
  }

  function renderModels() {
    if (form.mode !== 'module') return null
    if (!form.provider) {
      return jsx('div', { className: 'hc-agg__hint', 'data-agm-models-hint': 'no-provider',
                          children: '先选 provider，模型列表才会出现（两级联动）' })
    }
    var srcText = optionsView.source === 'config'
      ? '模型名来自该 Agent config.yaml 的 providers.<provider>.models'
      : optionsView.source === 'remote'
        ? '模型名来自上游 <base_url>/models（实时）'
        : optionsView.source === 'none'
          ? '该 provider 在 config.yaml 里没写 models 段，清单为空 ⇒ 请手填模型名（后端会标「未离线校验」）'
          : '正在读模型清单…'
    return jsxs('div', {
      key: 'models',
      className: 'hc-agg__model-models',
      'data-agm-models': String(models.length),
      'data-agm-models-source': optionsView.source || '',
      children: [
        jsx('div', { key: 'h', className: 'hc-agg__model-k', children: '模型（换 provider 会清空旧选择）' }),
        models.length
          ? jsx('select', {
              key: 'sel',
              className: 'hc-agg__model-select',
              'data-agm-model-select': '1',
              value: form.model,
              onChange: function (event) { onModelChange(event.target.value) },
              children: [jsx('option', { key: '__none', value: '', children: '— 请选择模型 —' }, '__none')].concat(
                models.map(function (name) {
                  return jsx('option', { key: name, value: name, children: name }, name)
                })
              )
            })
          : jsx('input', {
              key: 'free',
              type: 'text',
              className: 'hc-agg__model-input',
              'data-agm-model-input': '1',
              placeholder: '手填模型名（该 provider 没有模型清单）',
              value: form.model,
              onChange: function (event) { onModelChange(event.target.value.trim()) }
            }),
        jsx('div', { key: 'src', className: 'hc-agg__note', children: srcText })
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-agg__model',
    'data-agm-panel': '1',
    'data-agm-target': target,
    children: [
      jsxs('div', {
        key: 'head',
        className: 'hc-agg__block-head',
        children: [
          jsx('span', { key: 't', className: 'hc-agg__block-title', children: 'AI 模型（provider / model，按 Agent 独立）' }),
          chip('配置目录 auto-group/ai_model.json', 'path')
        ]
      }),

      // ---- 1. 选 Agent（或 global 段）----
      jsxs('div', {
        key: 'target',
        className: 'hc-agg__model-row',
        children: [
          jsx('label', { key: 'l', className: 'hc-agg__model-k', children: '配置对象' }),
          jsx('select', {
                      key: 's',
                      className: 'hc-agg__model-select',
                      'data-agm-target-select': '1',
                      value: target,
                      onChange: function (event) { pickTarget(event.target.value) },
                      children: (target
                        ? []
                        : [jsx('option', { key: '__pick', value: '', children: '— 请先选择 Agent —' }, '__pick')])
                        .concat((agents || []).map(function (agent) {
                          var has = Object.prototype.hasOwnProperty.call(declaredAgents, agent.id)
                          return jsx('option', {
                            key: agent.id,
                            value: agent.id,
                            children: (agent.label || agent.id) + (has ? '（已有独立声明）' : '')
                          }, agent.id)
                        }))
                        .concat([jsx('option', {
                          key: GLOBAL_TARGET,
                          value: GLOBAL_TARGET,
                          children: 'global 段（各 Agent 跟随自己 config.yaml · 此处不可指定模型）'
                        }, GLOBAL_TARGET)])
                    })
        ]
      }),

      noTarget
        ? jsx('div', {
            key: 'no-target',
            className: 'hc-agg__model-err',
            'data-agm-no-target': '1',
            children: '请先选择 Agent：provider/model 是按 Agent 的凭据与 config.yaml 校验的，' +
              '没有 Agent 就没有可配置的对象。（global 段只表示「各 Agent 跟随各自 config.yaml」，' +
              '本身不能指定具体模型）'
          })
        : null,
      autoPicked && target && !isGlobal
        ? jsx('div', {
            key: 'auto-target',
            className: 'hc-agg__model-note',
            'data-agm-auto-target': '1',
            children: '打开面板时没有从外层取到「当前 Agent」，已默认落在 Agent ' + target +
              '；要配别的 Agent 请在上方「配置对象」里切换。'
          })
        : null,

      dataView.phase === 'error'
        ? jsx(ErrorState, {
            key: 'load-error',
            title: '读取模型配置失败',
            message: dataView.error || '后端没有给原因',
            onRetry: function () { loadConfig(target) },
            retryLabel: '重新读取'
          })
        : null,
      dataView.phase === 'loading' && !dataView.data
        ? jsx('div', { key: 'loading', className: 'hc-agg__hint', 'data-agm-loading': '1',
                       children: '正在读取该 Agent 的模型配置…' })
        : null,

      // ---- 2. 来源层 + 实际生效值（这是「从哪来的」）----
      jsxs('div', {
        key: 'facts',
        className: 'hc-agg__model-facts',
        'data-agm-layer': resolved.layer || '',
        children: [
          kv('来源层',
            LAYER_TEXT[resolved.layer] || resolved.layer_label ||
              (dataView.phase === 'loading' ? '读取中…' : '未知（后端没给 layer）'), 'layer', 'layer'),
          kv('实际生效', (resolved.provider || '—') + ' / ' + (resolved.model || '—'), 'eff', 'effective'),
          kv('生效说明', resolved.reason || '—', 'why', 'reason'),
          kv('config.yaml 当前模型',
            (resolved.config_exists === false ? '（配置文件还没建）' : '') +
            ((data.agent_current && data.agent_current.provider) || '—') + ' / ' +
            ((data.agent_current && data.agent_current.model) || '—'), 'cur', 'agent_current'),
          data.config && data.config.error
            ? kv('配置读取错误', data.config.error, 'err', 'config_error')
            : null,
          data.config && data.config.legacy
            ? kv('格式', '盘上还是旧的单组格式（读取时已按 global 段 + 该 Agent 解释）；下次保存会升级为按 Agent 映射', 'legacy', 'legacy')
            : null
        ]
      }),

      // ---- 3. 模式（三态：该 Agent 独立 / 该 Agent 跟随全局 / 继承 global 段）----
      jsxs('div', {
        key: 'mode',
        className: 'hc-agg__model-row',
        children: [
          jsx('label', { key: 'l', className: 'hc-agg__model-k', children: '模式' }),
          jsxs('div', {
            key: 'opts',
            className: 'hc-agg__model-modes',
            children: [
              jsx('label', {
                key: 'module',
                className: 'hc-agg__model-mode' + (form.mode === 'module' ? ' hc-agg__model-mode--on' : ''),
                'data-agm-mode': 'module',
                title: isGlobal ? 'global 段不能写「独立模型」：独立配置必须点名一个 Agent（provider/model 是按该 Agent 的凭据校验的）' : '',
                children: [
                  jsx('input', {
                    key: 'r', type: 'radio', name: 'agm-mode', checked: form.mode === 'module',
                    disabled: isGlobal,
                    onChange: function () { onModeChange('module') }
                  }),
                  jsx('span', { key: 't', children: '这个 Agent 用独立模型' })
                ]
              }, 'module'),
              jsx('label', {
                key: 'global',
                className: 'hc-agg__model-mode' + (form.mode === 'global' ? ' hc-agg__model-mode--on' : ''),
                'data-agm-mode': 'global',
                title: '跟随该 Agent config.yaml 里当前生效的模型（不写 provider/model）',
                children: [
                  jsx('input', {
                    key: 'r', type: 'radio', name: 'agm-mode', checked: form.mode === 'global',
                    onChange: function () { onModeChange('global') }
                  }),
                  jsx('span', { key: 't', children: isGlobal
                    ? 'global 段声明为「跟随全局」'
                    : '这个 Agent 跟随它自己 config.yaml 的当前模型' })
                ]
              }, 'global'),
              isGlobal
                ? null
                : jsx('label', {
                    key: 'inherit',
                    className: 'hc-agg__model-mode' + (form.mode === 'inherit' ? ' hc-agg__model-mode--on' : ''),
                    'data-agm-mode': 'inherit',
                    title: '删掉这个 Agent 的声明，让它走 global 段（若 global 段也没声明，则回落它自己 config.yaml）',
                    children: [
                      jsx('input', {
                        key: 'r', type: 'radio', name: 'agm-mode', checked: form.mode === 'inherit',
                        onChange: function () { onModeChange('inherit') }
                      }),
                      jsx('span', { key: 't', children: decl.declared ? '删掉独立声明（改回继承）' : '继承（当前就没有独立声明）' })
                    ]
                  }, 'inherit')
            ]
          })
        ]
      }),

      // ---- 4. 两级联动：provider → model ----
      form.mode === 'module'
        ? jsxs('div', {
            key: 'two-level',
            className: 'hc-agg__model-twolevel',
            'data-agm-twolevel': '1',
            children: [
              jsxs('div', {
                key: 'provider',
                className: 'hc-agg__model-row',
                children: [
                  jsx('label', { key: 'l', className: 'hc-agg__model-k', children: 'provider' }),
                  jsx('select', {
                    key: 's',
                    className: 'hc-agg__model-select',
                    'data-agm-provider-select': '1',
                    value: form.provider,
                    onChange: function (event) { onProviderChange(event.target.value) },
                    children: [jsx('option', { key: '__none', value: '', children: '— 请选择 provider —' }, '__none')]
                      .concat(providers.map(function (row) {
                        return jsx('option', {
                          key: row.provider,
                          value: row.provider,
                          children: (row.label || row.provider) + (row.key_present ? '' : '（密钥未设置）')
                        }, row.provider)
                      }))
                  })
                ]
              }),
              renderModels()
            ]
          })
        : null,

      // ---- 4b. global 段不是「死页面」：明确说明它是什么、去哪配 ----
      isGlobal
        ? jsx('div', {
            key: 'global-note',
            className: 'hc-agg__model-note',
            'data-agm-global-note': '1',
            children: 'global 段 = 跟随各 Agent 自己的 config.yaml，此处不能指定具体 provider/model；' +
              '要指定请在上方「配置对象」里选一个 Agent（provider 清单也是按 Agent 的，' +
              '所以这里不列 provider 行）。'
          })
        : renderProviders(),

      // ---- 5. 保存 + 回读 ----
      jsxs('div', {
        key: 'save',
        className: 'hc-agg__toolbar',
        children: [
          jsx('button', {
                      key: 'go',
                      type: 'button',
                      className: 'hc-btn hc-btn--accent',
                      'data-agm-save': '1',
                      disabled: saveView.phase === 'busy' || !canSave,
                      title: isGlobal
                        ? 'global 段不写入：它没有自己的 provider/model（各 Agent 跟随各自 config.yaml）；要指定模型请先选一个 Agent'
                        : (noTarget
                          ? '请先选择 Agent'
                          : (!canSave
                            ? (inheritMode ? '继承模式不需要保存（后端没有「删除声明」的入参）' : '选了独立模式就必须同时给出 provider 与 model')
                            : '写盘后会重新读文件，界面只显示回读值（不把请求体当结果）')),
                      onClick: save,
                      children: saveView.phase === 'busy' ? '保存中…' : '保存并回读'
                    }),
          form.dirty
            ? jsx('span', { key: 'dirty', className: 'hc-agg__note', 'data-agm-dirty': '1',
                            children: '有未保存的改动' })
            : null,
          jsx('span', {
            key: 'hint',
            className: 'hc-agg__hint',
            children: isGlobal
                          ? '保存按钮为禁用态：global 段不写入任何内容。它 = 所有「没有独立声明」的 Agent 的回落层' +
                            '（各 Agent 跟随自己 config.yaml）；给具体 Agent 配独立模型请在上面选那个 Agent。'
                          : '只影响这个 Agent 的项目分组 AI；不会改它的 title_generation，也不会改别的 Agent。'
          })
        ]
      }),
      saveView.error
        ? jsx(ErrorState, { key: 'err', title: '保存失败', message: saveView.error,
                            onRetry: save, retryLabel: '重试保存' })
        : null,
      // 只有**真·回读成功**才带 data-agm-save-ok；「global 段不写入」是 note，不是成功提示。
            saveView.phase === 'done' && saveView.text
              ? jsx('div', { key: 'ok', className: 'hc-agg__scan-result', 'data-agm-save-ok': '1',
                             children: saveView.text })
              : null,
            saveView.phase === 'blocked' && saveView.text
              ? jsx('div', { key: 'blocked', className: 'hc-agg__model-note', 'data-agm-save-blocked': '1',
                             children: saveView.text })
              : null,
      data.recent_calls && data.recent_calls.length
        ? jsxs('div', {
            key: 'calls',
            className: 'hc-agg__model-calls',
            'data-agm-calls': String(data.recent_calls.length),
            children: [
              jsx('div', { key: 'h', className: 'hc-agg__model-k',
                           children: '最近真实调用（auto-group/ai_calls.jsonl，取最后 ' +
                             num(data.recent_calls.length) + ' 行）' }),
              jsx('div', {
                key: 'rows',
                className: 'hc-agg__model-calls-rows',
                children: data.recent_calls.slice().reverse().slice(0, 8).map(function (row, index) {
                  var when = row.ts ? new Date(row.ts * 1000).toLocaleString() : '—'
                  return jsx('div', {
                    key: 'c' + index,
                    className: 'hc-agg__note',
                    'data-agm-call': (row.provider || '') + '/' + (row.model || ''),
                    children: when + ' · agent=' + (row.agent || '—') + ' · ' +
                      (row.provider || '—') + '/' + (row.model || '—') +
                      ' · 层=' + (row.layer || row.source || '—') +
                      ' · ' + (row.ok ? '成功' : '失败') +
                      (row.error ? '（' + row.error + '）' : '') +
                      (row.elapsed_ms ? ' · ' + num(row.elapsed_ms) + 'ms' : '')
                  }, 'c' + index)
                })
              })
            ]
          })
        : jsx('div', { key: 'nocalls', className: 'hc-agg__hint', 'data-agm-calls': '0',
                       children: '还没有真实调用记录：跑一次「AI 分析」后这里会出现 provider/model/耗时（审计文件 auto-group/ai_calls.jsonl）。' })
    ]
  })
}

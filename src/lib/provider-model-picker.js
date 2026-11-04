/**
 * hermes-console · ProviderModelPicker —— 「提供商 + 模型」两级联动（**共享组件**，t_4d654faf）
 *
 * 老板的原话：控制台里所有用到大模型的地方，都要能选「提供商 + 模型」，顺序是
 *   先按「当前 Agent 的配置 / Hermes 全局」筛**可用 provider**，再按 provider 筛**模型**。
 *
 * 这个文件是**唯一**实现。任何分区要加"选模型"入口都挂它，不要各自再写一套 select。
 *
 * ## 公共契约（t_ac537221 的 autogroup 入口按这份契约接，别改签名）
 *
 * ```js
 * jsx(ProviderModelPicker, {
 *   agent,                    // 必填：以哪个 agent 的 provider 目录 / 模型清单为准
 *   value: {provider, model}, // 当前值（**来自回读的落盘真值**，不是本地草稿）
 *   onChange({provider, model}),   // provider 变了 ⇒ model 一定是 ''
 *   allowFollow: true,        // provider 下拉第一项是「跟随全局」（值 ''）
 *   followLabel: '跟随全局（继承 Hermes 全局配置）',
 *   allowEmptyModel: true,    // model 下拉第一项是「跟随该 provider 默认」（值 ''）
 *   sourceKind,               // 'agent' | 'global' | 'module_default'
 *   sourceText,               // 覆盖默认来源文案
 *   currentText,              // 覆盖「当前：…」那一行（一般给回读真值）
 *   advancedNote,             // 「高级」折叠区里的一句补充说明
 *   disabled, idPrefix,       // idPrefix 决定 data-pmp-* 属性，探针靠它定位
 * })
 * ```
 *
 * 硬规则（每条都有对应的断言，别"优化"掉）：
 *
 * 1. **provider 变了，旧 model 必须清空**：`onChange` 里 model 一律给 `''`，
 *    绝不允许把旧 provider 的模型带到新 provider 名下（后端也会再拒一次）。
 * 2. **model 列表只能来自当前选中的 provider**：`GET /models/models?provider=Y&agent=X`，
 *    缓存键里带 provider，换 provider 就是换 key（不会串数据）。
 * 3. **主路径是下拉**：手填模型名只在「高级」折叠区里，且必须显式勾选才提交
 *    （调用方据 `advanced` 决定是否带 `allow_unlisted_model: true`）。
 * 4. **保存后回读**：本组件不缓存自己的选择，`value` 由调用方从 GET 回来的真值喂进来；
 *    保存后调用方 `reload()` 再读一次，界面显示的就是落盘值。
 * 5. **密钥只显示存在性 + 变量名**：`key_env` 是变量**名**，`key_present` 是布尔；
 *    取值一个字节都不出现在这里（后端也只回 `***`）。
 *
 * 数据来源（全部是既有接口，不新增）：
 *   GET /models/providers?agent=X             → providers[]（provider / label / key_env / key_present）
 *   GET /models/models?provider=Y&agent=X     → models[]（source=config|remote|config+none）
 *   GET /models/validate?agent=X&provider=Y&model=Z  → 保存前的成对校验（非法 ⇒ 400）
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

import { get } from './rest.js'
import { useQuery } from './query.js'

/** 缓存键前缀：换 agent / provider 就换 key，不会把上一个 provider 的模型串过来。 */
export var PICKER_CACHE_PREFIX = 'picker/'

/**
 * 模型条目的来源徽标：后端 `model_entries[].source` 的三种取值
 * （`config` / `live` / `config+live`，见 `dashboard/api_models.py::_model_entries`）。
 * 下拉里**逐条**标出来源 —— 「配置里手写的」和「供应商实时返回的」不能混成一个数字。
 */
export var MODEL_SOURCE_LABELS = {
  config: '配置',
  live: '实时',
  'config+live': '配置+实时'
}

/**
 * 模型清单来源（后端 `source`）的整段文案：`config` / `remote` / `config+remote` / `none`。
 * 与 `source_label` 同义，但这里写死一份，免得为了一个文案依赖后端两套字段。
 */
export var MODEL_LIST_SOURCE_LABELS = {
  config: '配置文件（只读 config，没有出网）',
  remote: '供应商实时返回',
  'config+remote': '配置文件 + 供应商实时返回',
  none: '没有来源（配置没列，实时也没拉到）'
}

/** 来源层的默认文案（三态，与后端 `/models/usage` 的 `source` 同一套词）。 */
export var SOURCE_LABELS = {
  agent: '该 Agent 独立配置（只对这个 agent 生效）',
  global: '跟随 Hermes 全局（继承主模型 / 默认值）',
  module_default: '模块默认（该模块内置默认值）'
}

/**
 * 来源层文案。`kind` 缺省时按 provider 推：`auto` / 空 ⇒ 跟随全局，否则算该 agent 独立配置。
 * @param {string} kind
 * @param {string} agent
 * @param {string} provider
 * @returns {string}
 */
export function describeSource(kind, agent, provider) {
  var text = String(kind || '')
  if (!text) text = !provider || String(provider).toLowerCase() === 'auto' ? 'global' : 'agent'
  if (text === 'agent' && agent) return agent + ' 独立配置（只对这个 agent 生效）'
  return SOURCE_LABELS[text] || SOURCE_LABELS.agent
}

function payloadOf(query) {
  var body = query && query.data
  return body && typeof body === 'object' ? body : null
}

function textOf(value) {
  return value === undefined || value === null ? '' : String(value)
}

/**
 * 两级联动选择器。
 * @param {{agent: string, value?: {provider?: string, model?: string},
 *   onChange?: Function, allowFollow?: boolean, followLabel?: string,
 *   allowEmptyModel?: boolean, sourceKind?: string, sourceText?: string,
 *   currentText?: string, advancedNote?: string, disabled?: boolean,
 *   idPrefix?: string, label?: string}} props
 */
export function ProviderModelPicker(props) {
  var source = props || {}
  var agent = textOf(source.agent)
  var value = source.value && typeof source.value === 'object' ? source.value : {}
  var provider = textOf(value.provider)
  var model = textOf(value.model)
  var disabled = source.disabled === true
  var allowFollow = source.allowFollow === true
  var allowEmptyModel = source.allowEmptyModel !== false
  var idp = textOf(source.idPrefix) || 'pmp'
  var advancedState = useState(false)
  var advanced = advancedState[0] === true
  var advancedModelState = useState(model)
  var advancedModel = advancedModelState[0]
  var advancedDirtyState = useState(false)
  var advancedDirty = advancedDirtyState[0] === true
  /**
   * 实时拉取开关。默认 **true**：下拉要显示的是「供应商真实可用的模型」，
   * 不是 config.yaml 里手写的那几条（与模型库 `models.js` 的 `libLive` 同一口径）。
   * 关掉只是「只看配置、不出网」，后端默认 `live=0` 的语义。
   */
  var liveState = useState(true)
  var live = liveState[0] !== false
  /**
   * 手动刷新计数器。点「刷新」⇒ nonce+1 ⇒ **缓存键变化** ⇒ 一定发新请求，
   * 且带 `refresh=1`（后端无条件绕过 TTL 缓存）。不用 `reload()`：
   * 那个只会重取同一个 key，且不带 `refresh=1`，命中后端缓存就看不到新东西。
   */
  var nonceState = useState(0)
  var nonce = nonceState[0]
  /**
   * 「高级：手填模型名」的后端校验状态（`GET /models/validate`，只读）。
   * `phase`: idle（还没校验 / 输入已变）| checking | ok | error。
   * 失败原因必须显示在选择器下方 —— 清单外的名字是唯一能绕过清单的入口，不能静默放行。
   */
  var validateState = useState({ phase: 'idle', message: '', model: '' })
  var validation = validateState[0]

  var providersQuery = useQuery(
    PICKER_CACHE_PREFIX + 'providers/' + agent,
    function () {
      return get('/models/providers', { params: { agent: agent }, timeoutMs: 20000 })
    },
    { staleMs: 30000, enabled: Boolean(agent) }
  )
  var providersBody = payloadOf(providersQuery)
  var providers = providersBody && Array.isArray(providersBody.providers) ? providersBody.providers : []

  var modelsQuery = useQuery(
    PICKER_CACHE_PREFIX + 'models/' + agent + '/' + provider + '/' + (live ? 'live' : 'config') + '/' + nonce,
    function () {
      // ★ `live=1`：下拉要的是**供应商真实可用的模型**。后端默认 `live=0`（只读 config、不出网）
      //   是有意设计，不能改；所以必须在这里显式要求实时拉取。
      //   `refresh=1`：手动刷新，后端无条件绕过 TTL 缓存。
      var params = { provider: provider, agent: agent }
      if (live) params.live = '1'
      if (nonce > 0) params.refresh = '1'
      return get('/models/models', {
        params: params,
        timeoutMs: 20000
      })
    },
    // provider 是 auto / 空 ⇒ **没有模型清单可读**（auto 不是真 provider，后端会 400）：
    // 这时模型跟随主模型，下拉只留「跟随主模型」一项。
    { staleMs: 30000, enabled: Boolean(agent) && Boolean(provider) && provider.toLowerCase() !== 'auto' }
  )
  var modelsBody = payloadOf(modelsQuery)
  var models = modelsBody && Array.isArray(modelsBody.models) ? modelsBody.models : []
  var modelSource = modelsBody && modelsBody.source ? String(modelsBody.source) : ''
  /**
   * 逐条来源（后端 `model_entries[].source`）。`models` 保持「纯名字字符串数组」的形状不动
   * （缓存键 / `data-pmp-model-count` / 当前值比对都依赖它），来源另开一张 name→source 表。
   */
  var entrySourceOf = {}
  if (modelsBody && Array.isArray(modelsBody.model_entries)) {
    modelsBody.model_entries.forEach(function (entry) {
      var name = textOf(entry && entry.name)
      if (name) entrySourceOf[name] = textOf(entry && entry.source)
    })
  }
  var liveInfo = modelsBody && modelsBody.live && typeof modelsBody.live === 'object' ? modelsBody.live : null
  var liveRequested = Boolean(liveInfo && liveInfo.requested)
  var liveOk = Boolean(liveInfo && liveInfo.ok)
  var liveErrorKind = textOf(liveInfo && liveInfo.error_kind)
  var liveError = textOf(modelsBody && modelsBody.live_error) ||
    textOf(liveInfo && liveInfo.error)
  var liveCount = modelsBody && typeof modelsBody.live_count === 'number' ? modelsBody.live_count : 0
  var configCount = modelsBody && typeof modelsBody.config_count === 'number' ? modelsBody.config_count : 0
  /** 实时拉取失败：`requested` 但 `ok=false` —— 必须显式报错，不许静默只剩配置项。 */
  var liveFailed = liveRequested && !liveOk

  /**
   * 下拉的**默认口径**：`<select>` 里**只放**供应商实时成功返回的模型（卡面硬要求）。
   *
   * `models` 是后端按 `model_entries` 顺序展开的**并集**（配置项在前、实时独有项在后），
   * 把并集直接塞进下拉，等于让 config.yaml 里的陈旧项冒充「供应商当前可用」——
   * balance 会从 37 变 39、gemini 从 44 变 63。所以这里按逐条来源分成两组：
   *
   *   * `mainModels`：`source ∈ {live, config+live}` —— 供应商这次**真返回了**，进 `<select>`；
   *   * `configOnlyModels`：`source === 'config'` —— 只在 config.yaml 里，
   *     供应商**没返回**。它们**不放进 `<select>`**（连 `<optgroup>` 也不行：
   *     只要在 select 里，DOM options 就还是 39/63，验收必挂），
   *     改为下拉**外**一行只读提示 + 可展开列表（`data-pmp-config-only`）。
   *
   * 实时拉取失败时：一条实时结果都没有 ⇒ `mainModels` 为空 ⇒ `<select>` 里只剩占位项，
   * **绝不回退成配置列表冒充实时结果**（失败的配置项全部落到下拉外的「未返回」清单里）。
   *
   * 「只看配置」（`live=false`，用户显式关掉出网）是另一回事：那时没有实时结果可比，
   * 清单本身**就是**配置清单，全部进主列表，也不该挂「供应商未返回」的标注。
   * 虚拟 provider（moa 预设名）没有 `model_entries`，同样全部进主列表。
   */
  var entrySourceListed = Boolean(
    modelsBody && Array.isArray(modelsBody.model_entries) && modelsBody.model_entries.length
  )
  /**
   * 「正在拉取」的**确定性**判定：走 `status === 'loading'`（React 状态），不是
   * `busy`（模块级 inflight 表，渲染时机不受控 —— 实测点刷新后 400ms 按钮仍显示「刷新」）。
   * provider 换了就是换了 key，新 key 没有缓存 ⇒ status 必为 loading ⇒ loading 一定显示，
   * 也一定**不会**拿上一个 provider 的旧列表顶（旧 key 的数据不参与本次渲染）。
   */
  var loading = modelsQuery.status === 'loading'
  var mainModels = []
  var configOnlyModels = []
  models.forEach(function (name) {
    var one = String(name)
    var oneSource = textOf(entrySourceOf[one])
    var isLiveBacked = oneSource === 'live' || oneSource === 'config+live'
    if (!live || !entrySourceListed || isLiveBacked) mainModels.push(one)
    else configOnlyModels.push(one)
  })

  var providerRow = null
  for (var index = 0; index < providers.length; index += 1) {
    if (textOf(providers[index] && providers[index].provider) === provider) providerRow = providers[index]
  }
  /** provider 是 auto / 空 ⇒ 跟随主模型（没有自己的模型清单）。 */
  var followValue = textOf(source.followValue)
  var isFollow = !provider || provider.toLowerCase() === 'auto' ||
    (followValue !== '' && provider === followValue)

  var emit = function (nextProvider, nextModel, advancedFlag) {
    if (typeof source.onChange !== 'function') return
    source.onChange({ provider: nextProvider, model: nextModel, advanced: advancedFlag === true })
  }

  var onProvider = function (event) {
    var next = textOf(event.target.value)
    if (next === provider) return
    // ★ 硬规则 1：provider 变了，旧 model 立即清空（旧模型属于旧 provider）。
    advancedModelState[1]('')
    advancedDirtyState[1](false)
    emit(next, '', false)
  }

  var onModel = function (event) {
    emit(provider, textOf(event.target.value), false)
  }

  var onAdvancedToggle = function (event) {
    var open = Boolean(event.target && event.target.open)
    advancedState[1](open)
    if (open) {
      advancedModelState[1](model)
      advancedDirtyState[1](false)
    }
  }

  var onAdvancedModel = function (event) {
    advancedModelState[1](textOf(event.target.value))
    advancedDirtyState[1](true)
    // 输入变了 ⇒ 上一次的校验结论作废（否则旧结论会贴在一个已经不同的名字上）。
    validateState[1]({ phase: 'idle', message: '', model: '' })
  }

  /**
   * 「用这个名字」= 交付一个**清单外的模型名**。交付前先调后端只读校验：
   *   `GET /models/validate?agent=&provider=&model=`（`dashboard/api_models.py::validate`）
   *
   * 为什么要这一步：清单外的名字是唯一绕过「provider 的模型清单」的入口，后端写入口
   * 默认会 400 拒绝（`value_rejected`）。提前校验能把这个 400 变成「在选择器下方当场看到
   * 原因」，而不是保存到一半才炸。**只读**：这个请求不写任何配置。
   *
   * 带 `allow_unlisted=1`：调用方本来就会以 `allow_unlisted_model: true` 提交，校验口径
   * 必须与写入口一致；校验不通过（例如 provider 不认识 / 名字非法）时后端回 400 + 可读
   * `message`，这里**原样显示、不吞**，并且**不放行**（不 emit）。
   *
   * 后端没起 / 网络失败时同样拦住并显式说原因：宁可让用户重试，也不要假装校验过。
   */
  var applyAdvanced = function () {
    var next = textOf(advancedModel).trim()
    if (!next) {
      validateState[1]({ phase: 'error', message: '模型名不能为空：手填要给出具体名字', model: next })
      return
    }
    if (validateState[0].phase === 'checking') return
    validateState[1]({ phase: 'checking', message: '', model: next })
    get('/models/validate', {
      params: { agent: agent, provider: provider, model: next, allow_unlisted: '1' },
      timeoutMs: 20000
    }).then(function (result) {
      if (result && result.ok === false) {
        validateState[1]({
          phase: 'error',
          message: '校验没过，没有应用：' + (result.message || '后端没有给原因'),
          model: next
        })
        return
      }
      var body = result && result.data && typeof result.data === 'object' ? result.data : {}
      var note = textOf(body.note)
      validateState[1]({
        phase: 'ok',
        message: body.unverified
          ? '后端校验通过，但该名字不在 ' + provider + ' 的清单里 ⇒ 会记为未校验。' + (note ? ' ' + note : '')
          : '后端校验通过。' + (note ? ' ' + note : ''),
        model: next
      })
      advancedDirtyState[1](false)
      emit(provider, next, true)
    })
  }

  var providerOptions = []
  if (allowFollow) {
    providerOptions.push(
      jsx(
        'option',
        { value: followValue, children: textOf(source.followLabel) || '跟随全局（继承 Hermes 全局配置）' },
        'follow'
      )
    )
  }
  var known = {}
  providers.forEach(function (row) {
    var name = textOf(row && row.provider)
    if (!name) return
    known[name] = true
    var label = textOf(row && row.label) || name
    var kind = row && row.kind === 'virtual' ? '（虚拟）' : ''
    providerOptions.push(
      jsx('option', { value: name, children: label + kind + ' · ' + name }, 'p-' + name)
    )
  })
  // 当前值不在 provider 目录里（``auto`` / 已下线 / 还没声明的）⇒ 照样列出来：
  // 否则原生 select 会静默落到第一项，界面显示的和落盘值就不是一个东西了。
  if (provider && !known[provider] && provider !== followValue) {
    providerOptions.push(
      jsx('option', { value: provider, children: provider + '（当前值）' }, 'p-cur')
    )
  }

  var modelOptions = []
  if (isFollow) {
    modelOptions.push(
      jsx('option', { value: '', children: '（跟随主模型：不需要选模型）' }, 'm-follow')
    )
  } else if (allowEmptyModel) {
    modelOptions.push(
      jsx('option', { value: '', children: '（跟随该 provider 的默认模型）' }, 'm-default')
    )
  }
  /**
   * ★ 正在实时拉取 ⇒ 下拉里给一条**不可选**的占位项（`data-model-group="loading"`）。
   * 不给它的话下拉在这个间隙是空的，看起来像「这个 provider 一个模型都没有」——
   * 而真相是「还没拉回来」。两个状态的界面必须不同。
   */
  if (loading && !models.length) {
    modelOptions.push(
      jsx(
        'option',
        {
          value: '',
          disabled: true,
          'data-model-group': 'loading',
          children: '正在实时拉取 ' + provider + ' 的模型清单…'
        },
        'm-loading'
      )
    )
  }
  /**
   * 单条 `<option>`：**只有在主列表里**的项会走这里（即供应商实时返回的那些）。
   * `data-model-source` 是逐条来源（live / config+live），探针靠它证明「37 条全部来自实时」。
   * `data-model-selected` / 文案后缀「（当前选中）」把**当前选中项**在列表里标出来 ——
   * 光靠 `<select value>` 只能看到一行，看不出它在 37 条里处于什么位置、是不是还活着。
   */
  var modelOption = function (text) {
    var entrySource = textOf(entrySourceOf[text])
    var badge = MODEL_SOURCE_LABELS[entrySource] || ''
    var isCurrent = textOf(text) === model && !isFollow
    return jsx(
      'option',
      {
        value: text,
        'data-model-source': entrySource,
        'data-model-group': 'live',
        'data-model-selected': isCurrent ? '1' : '0',
        // 逐条标来源 + 当前选中：配置里手写的 / 供应商实时返回的 / 现在用的那一条，同一个下拉里也分得清。
        children: text + (badge ? '（' + badge + '）' : '') + (isCurrent ? '（当前选中）' : '')
      },
      'm-' + text
    )
  }
  mainModels.forEach(function (name) {
    modelOptions.push(modelOption(String(name)))
  })
  /**
   * ★ 当前值落在「配置独有」批次里 ⇒ 主下拉**不因它增加 option**（卡面硬要求）。
   *
   * 但用户必须看得见自己的当前值，否则会以为「没设置」。做法：占位项文案
   * 直接说明当前值是什么、以及它「供应商这次没返回」，而不是塞一条 option 进去。
   * 当前值若既不在清单里、也不在配置独有批次里（手填的上游新名字），同理。
   */
  var currentValueOffList = Boolean(model) && !isFollow && mainModels.indexOf(model) === -1
  if (currentValueOffList && modelOptions.length && textOf(modelOptions[0].props && modelOptions[0].props.value) === '') {
    modelOptions[0] = jsx(
      'option',
      {
        value: '',
        'data-model-group': 'placeholder',
        children: '（当前值 ' + model + ' 不在供应商实时清单里 · 想保留就选这一项或手填）'
      },
      'm-default'
    )
  }

  /**
   * 「另有 N 项已配置但供应商未返回」—— **在主下拉之外**。
   *
   * 卡面口径收紧了两次，这里是最终形态：
   *   * `<select>` 里**一条**配置独有项都不能有（optgroup 也不行，DOM options 会变成 39/63）；
   *   * 它们以一行只读提示 + `<details>` 可展开清单呈现，带 `data-pmp-config-only` /
   *     `data-pmp-config-only-count` 供探针精确计数；
   *   * 当前值若落在这批里，清单里那一条明确标「当前值」（`data-pmp-config-only-current`），
   *     主下拉条数不变。
   */
  var currentInConfigOnly = Boolean(model) && configOnlyModels.indexOf(model) > -1
  var configOnlyBlock = configOnlyModels.length
    ? jsxs(
        'div',
        {
          'data-pmp-config-only': idp,
          'data-pmp-config-only-count': String(configOnlyModels.length),
          style: {
            display: 'flex',
            flexDirection: 'column',
            gap: 'var(--s1)',
            fontSize: 'var(--fs-xs)',
            color: 'var(--ui-muted)',
            minWidth: 0
          },
          children: [
            jsx('span', {
              'data-pmp-config-only-summary': idp,
              style: { fontWeight: 600, overflowWrap: 'anywhere' },
              children:
                '另有 ' + configOnlyModels.length + ' 项已配置但供应商未返回（= 已失效，不要选）' +
                (currentInConfigOnly
                  ? ' · 当前值 ' + model + ' 已失效（在主下拉之外，选不到它）'
                  : '')
            }, 'sum'),
            jsxs(
              'details',
              {
                'data-pmp-config-only-details': idp,
                children: [
                  jsx('summary', {
                    style: { cursor: 'pointer', color: 'var(--ui-muted)' },
                    children: '展开这 ' + configOnlyModels.length + ' 项（只读，不会进主下拉）'
                  }, 's'),
                  jsx(
                    'div',
                    {
                      'data-pmp-config-only-list': idp,
                      style: {
                        display: 'flex',
                        flexWrap: 'wrap',
                        gap: 'var(--s1)',
                        marginTop: 'var(--s1)',
                        fontFamily: 'var(--font-mono, monospace)'
                      },
                      children: configOnlyModels.map(function (name) {
                        return jsx(
                          'span',
                          {
                            'data-pmp-config-only-item': String(name),
                            'data-pmp-config-only-current': String(name) === model ? '1' : '0',
                            style: {
                              border: 'var(--hairline) solid currentColor',
                              borderRadius: 'var(--r1, 4px)',
                              padding: '0 var(--s1)',
                              fontWeight: String(name) === model ? 700 : 400
                            },
                            children: String(name) === model ? name + '（当前值）' : String(name)
                          },
                          'cfg-' + name
                        )
                      })
                    },
                    'list'
                  )
                ]
              },
              'details'
            )
          ]
        },
        'config-only'
      )
    : null

  var sourceKind = textOf(source.sourceKind)
  var sourceText =
    textOf(source.sourceText) || describeSource(sourceKind, agent, provider)
  /**
   * 「当前选中 / 已失效」的一句话结论。**必须和下拉里的标注同源**：
   * 下拉负责把选中的那一条挑出来（`data-model-selected`），这一行负责把
   * 「它还在不在供应商清单里」说出来 —— 只显示名字等于没说它是否还有效。
   */
  var currentStateText = isFollow
    ? '（跟随全局：模型由主模型决定）'
    : !model
      ? '（未选具体模型：用该 provider 的默认模型）'
      : currentValueOffList
        ? '· 已失效：不在 ' + provider + ' 当前实时清单里'
        : '· 已选中：在 ' + provider + ' 当前实时清单里'
  var currentText =
    textOf(source.currentText) ||
    (isFollow
      ? '当前：继承全局（provider=auto ⇒ 模型跟随主模型）'
      : model
        ? '当前：' + model + ' · ' + provider + ' ' + currentStateText
        : '当前：provider ' + provider + ' · 模型跟随该 provider 默认')

  var keyText = ''
  if (providerRow) {
    var keyEnv = textOf(providerRow.key_env)
    if (keyEnv) keyText = 'key_env: ' + keyEnv + (providerRow.key_present ? '（已设置）' : '（未设置）')
    else if (providerRow.kind === 'virtual') keyText = '虚拟 provider：不需要密钥'
  }

  /**
   * 模型清单状态行。
   *
   * 三种必须**分得清**的状态：
   *   * 实时拉成功 ⇒ 说清「几个（配置 X / 实时 Y），来源=…」+ 是否命中缓存；
   *   * 实时拉失败 ⇒ **显式报错**（`error_kind` + 后端原文），绝不许静默只剩配置项 ——
   *     用户看到的那几条是配置里的，不是供应商真实可用的，这件事必须写在脸上；
   *   * 只看配置（`live=false`）⇒ 明说「没有出网」，别让人以为是供应商只有这几条。
   */
  var modelHint = ''
  if (!provider) modelHint = '先选提供商'
  else if (isFollow) modelHint = '跟随全局：模型由主模型决定，这里不需要选'
  else if (loading && !models.length) modelHint = '正在实时拉取 ' + provider + ' 的模型清单…'
  else if (modelsQuery.status === 'error') modelHint = '读取模型清单失败（' + provider + '）：' + modelsQuery.error
  else if (liveFailed) {
    modelHint = '实时拉取失败（' + (liveErrorKind || 'unknown') + '）：' + (liveError || '后端没有给出原因')
  } else if (!models.length) {
    modelHint = live
      ? '该 provider 在 config.yaml 里没有列出模型，实时也没拉到（可点「刷新」或手填）'
      : '该 provider 在 config.yaml 里没有列出模型（当前只看配置；可点「刷新」实时拉取或手填）'
  } else if (!live) {
    modelHint = configCount + ' 个模型（只看配置，未出网）'
  } else {
    // ★ 主列表只数「供应商真返回的」；配置独有项在主下拉**之外**单独列，不混进主列表的数字里。
    modelHint = mainModels.length + ' 个模型（供应商实时返回）' +
      (configOnlyModels.length
        ? ' · 另有 ' + configOnlyModels.length + ' 项已配置但供应商未返回（在下拉外单独列出，不进下拉）'
        : '') +
      ' · 来源：' + (MODEL_LIST_SOURCE_LABELS[modelSource] || modelSource || 'unknown') +
      (liveInfo && liveInfo.cached ? ' · 命中缓存（点刷新可强制重取）' : '')
  }
  var modelHintTone = liveFailed ? { color: 'var(--ui-danger, inherit)' } : null

  var rowStyle = { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center', minWidth: 0 }
  var labelStyle = { fontSize: 'var(--fs-xs)', minWidth: '4em', color: 'var(--ui-muted)' }

  return jsxs(
    'div',
    {
      className: 'hc-pmp',
      'data-pmp': idp,
      'data-pmp-agent': agent,
      'data-pmp-provider-value': provider,
      'data-pmp-model-value': model,
      'data-pmp-source': sourceKind || (provider ? 'agent' : 'global'),
      'data-pmp-model-count': String(models.length),
      // ★ 主列表 = 供应商实时返回的那些（配置独有项另组，不计入这里）。
      'data-pmp-main-count': String(mainModels.length),
      'data-pmp-config-only-count': String(configOnlyModels.length),
      'data-pmp-live': live ? '1' : '0',
      'data-pmp-live-requested': liveRequested ? '1' : '0',
      'data-pmp-live-ok': liveRequested ? (liveOk ? '1' : '0') : '',
      'data-pmp-live-error-kind': liveErrorKind,
      'data-pmp-loading': loading ? '1' : '0',
      /**
       * 当前值的状态，三档之外还有一个「失效」档：
       *   `selected` = 当前值就在供应商实时清单里（能选、能用）；
       *   `invalid`  = 当前值不在清单里（已失效 / 手填的清单外名字）—— 必须能看出来；
       *   `empty`    = 没选模型（跟随该 provider 默认）；
       *   `follow`   = 跟随全局。
       */
      'data-pmp-current-state': isFollow
        ? 'follow'
        : !model
          ? 'empty'
          : currentValueOffList
            ? 'invalid'
            : 'selected',
      'data-pmp-live-count': String(liveCount),
      'data-pmp-config-count': String(configCount),
      'data-pmp-model-source': modelSource,
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
      // ★ children 必须写在 props 里：jsx-runtime 的第三个位置参数是 **key**，
      //   把数组塞在那里 = 一个子节点都不渲染（这个 bug 被 UI 探针当场抓住）。
      children: [
      jsxs(
        'div',
        {
          style: rowStyle,
          children: [
            jsx('span', { style: labelStyle, children: '提供商' }, 'lbl-provider'),
            jsxs(
              'select',
              {
                className: 'hc-select',
                'data-pmp-provider': idp,
                value: provider,
                disabled: disabled || !agent,
                onChange: onProvider,
                children: providerOptions
              },
              'sel-provider'
            )
          ]
        },
        'row-provider'
      ),
      jsxs(
        'div',
        {
          style: rowStyle,
          children: [
            jsx('span', { style: labelStyle, children: '模型' }, 'lbl-model'),
            jsxs(
              'select',
              {
                className: 'hc-select',
                'data-pmp-model': idp,
                // 当前值不在主下拉里（配置独有 / 清单外手填）⇒ 原生 select 无法回显一个不存在的
                // option，只会显示空白。所以把 value 置空，让第一项占位文案把「当前值是什么」
                // 直接说出来（**不加 option**，条数不变）。
                value: currentValueOffList ? '' : model,
                disabled: disabled || !provider || isFollow,
                onChange: onModel,
                children: modelOptions
              },
              'sel-model'
            )
          ]
        },
        'row-model'
      ),
      jsxs(
        'div',
        {
          'data-pmp-controls': idp,
          style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center', minWidth: 0 },
          children: [
            jsx(
              'button',
              {
                type: 'button',
                className: 'hc-btn hc-btn--tiny',
                'data-pmp-refresh': idp,
                'data-pmp-refreshing': loading ? '1' : '0',
                disabled: disabled || !provider || isFollow || loading,
                title: '重新向该 provider 拉取模型清单（请求带 live=1&refresh=1，绕过缓存）',
                onClick: function () {
                  // ★ 手动刷新：nonce+1 ⇒ 缓存键变化 ⇒ 一定发新请求且带 refresh=1。
                  nonceState[1](function (n) {
                    return n + 1
                  })
                },
                // 文案带 provider 名：一眼看出这次刷的是哪一个（切换后列表不变时，
                // 第一件要排查的就是「到底有没有为新 provider 发请求」）。
                children: loading && provider ? '刷新中…（' + provider + '）' : '刷新模型',
              },
              'refresh'
            ),
            jsx(
              'label',
              {
                className: 'hc-muted',
                style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--s1)', fontSize: 'var(--fs-xs)' },
                children: [
                  jsx('input', {
                    type: 'checkbox',
                    'data-pmp-live-toggle': idp,
                    checked: live,
                    disabled: disabled,
                    onChange: function (event) {
                      liveState[1](event.target.checked !== false)
                    }
                  }, 'cb'),
                  jsx('span', {
                    children: live ? '实时拉取（live=1，出网）' : '只看配置（live=0，不出网）'
                  }, 't')
                ]
              },
              'livetoggle'
            )
          ]
        },
        'controls'
      ),
      jsx(
        'div',
        {
          className: 'hc-muted',
          'data-pmp-hint': idp,
          style: modelHintTone
            ? { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', color: modelHintTone.color, fontWeight: 600 }
            : { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
          children: modelHint
        },
        'hint'
      ),
      // ★ 配置独有项：**在下拉之外**（一行只读提示 + 可展开清单）。
      //   卡面明确禁止放进 `<select>`（连 optgroup 也不行），否则 DOM options 变 39/63。
      configOnlyBlock,
      // ★ 实时拉取失败 ⇒ **单独一块红字**把 `error_kind` 与后端原文摆出来。
      //   绝不静默只剩配置项：用户必须知道「下面这些是配置里的，不是供应商真实可用的」。
      liveFailed
        ? jsxs(
            'div',
            {
              'data-pmp-live-error': idp,
              'data-pmp-live-error-kind': liveErrorKind,
              role: 'alert',
              style: {
                fontSize: 'var(--fs-xs)',
                color: 'var(--ui-danger, inherit)',
                border: 'var(--hairline) solid currentColor',
                borderRadius: 'var(--r1, 4px)',
                padding: 'var(--s1) var(--s2)',
                overflowWrap: 'anywhere',
                minWidth: 0
              },
              children: [
                jsx('span', { children: '实时拉取失败 · ' + (liveErrorKind || 'unknown') }, 'k'),
                jsx('span', {
                  'data-pmp-live-error-text': idp,
                  style: { display: 'block', fontWeight: 400 }
                }, 'txt'),
                jsx('span', {
                  'data-pmp-live-error-detail': idp,
                  style: { display: 'block', fontWeight: 400 },
                  children: liveError || '（后端没有给出原因）'
                }, 'det')
              ]
            },
            'live-error'
          )
        : null,
      jsxs(
        'div',
        {
          'data-pmp-status': idp,
          style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', fontSize: 'var(--fs-xs)', minWidth: 0 },
          children: [
            jsx('span', { 'data-pmp-current': idp, children: currentText }, 'cur'),
            jsx(
              'span',
              {
                'data-pmp-source-text': idp,
                style: { color: 'var(--ui-muted)', overflowWrap: 'anywhere' },
                children: '来源：' + sourceText
              },
              'src'
            ),
            keyText
              ? jsx(
                  'span',
                  {
                    'data-pmp-keyenv': idp,
                    style: { color: 'var(--ui-muted)' },
                    title: '凭据只读：这里只显示变量名与是否已设置，绝不回显取值',
                    children: keyText
                  },
                  'key'
                )
              : null
          ]
        },
        'status'
      ),
      jsxs(
        'details',
        {
          'data-pmp-advanced': idp,
          onToggle: onAdvancedToggle,
          style: { minWidth: 0 },
          children: [
            jsx('summary', {
              style: { fontSize: 'var(--fs-xs)', color: 'var(--ui-muted)', cursor: 'pointer' },
              children: '高级：手填模型名（清单外的名字）'
            }, 'sum'),
            jsxs(
              'div',
              {
                style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center', marginTop: 'var(--s1)' },
                children: [
                  jsx('input', {
                    className: 'hc-input',
                    type: 'text',
                    'data-pmp-advanced-input': idp,
                    value: advancedModel,
                    disabled: disabled,
                    placeholder: '清单外的模型名（会以 allow_unlisted_model 提交）',
                    onChange: onAdvancedModel
                  }, 'inp'),
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn hc-btn--tiny',
                    'data-pmp-advanced-apply': idp,
                    'data-pmp-advanced-checking': validation.phase === 'checking' ? '1' : '0',
                    disabled: disabled || !advancedDirty || validation.phase === 'checking',
                    onClick: applyAdvanced,
                    children: validation.phase === 'checking' ? '校验中…' : '用这个名字'
                  }, 'btn')
                ]
              },
              'body'
            ),
            jsx('div', {
              className: 'hc-muted',
              style: { fontSize: 'var(--fs-xs)', marginTop: 'var(--s1)', overflowWrap: 'anywhere' },
              children: textOf(source.advancedNote) ||
                '主路径是下拉；这里只给"该 provider 的清单里确实没有、但上游认这个名字"的情况。' +
                  '点「用这个名字」前会先调只读校验（GET /models/validate），通过后调用方才会以 ' +
                  'allow_unlisted_model=true 保存；后端会把它记为未校验。'
            }, 'note'),
            // ★ 校验结论：失败必须**留在可见处**（这是清单外名字唯一的把关点，不能只写 console）。
            validation.phase === 'idle'
              ? null
              : jsx('div', {
                  'data-pmp-validate': idp,
                  'data-pmp-validate-phase': validation.phase,
                  'data-pmp-validate-model': validation.model,
                  'data-pmp-validate-error': validation.phase === 'error' ? '1' : '0',
                  role: validation.phase === 'error' ? 'alert' : undefined,
                  style: {
                    fontSize: 'var(--fs-xs)',
                    marginTop: 'var(--s1)',
                    overflowWrap: 'anywhere',
                    fontWeight: validation.phase === 'error' ? 600 : 400,
                    color: validation.phase === 'error' ? 'var(--ui-danger, inherit)' : 'var(--ui-muted)'
                  },
                  children: validation.phase === 'checking'
                    ? '正在向后端校验 ' + validation.model + ' …'
                    : validation.message
                }, 'validate')
          ]
        },
        'advanced'
      ),
      advanced && advancedDirty
        ? jsx('div', {
            className: 'hc-muted',
            'data-pmp-advanced-dirty': idp,
            style: { fontSize: 'var(--fs-xs)' },
            children: '高级值待应用：' + textOf(advancedModel) + '（点「用这个名字」才生效）'
          }, 'adv-dirty')
        : null
      ]
    }
  )
}

export default ProviderModelPicker

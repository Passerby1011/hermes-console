/**
 * hermes-console · 外壳（左栏分区导航 + 顶栏作用域 + 内容区三态）。
 *
 * 导航**不再硬编码**：启动与切作用域时拉 `GET /modules?agent=<当前作用域>`，只渲染
 * `enabled === true` 的模块、按 `nav_order` 升序 —— 所以「删一个模块目录就少一个 tab」
 * 在界面上真正成立。组件是打包产物、必须静态可达，因此 id → 组件的字面量映射
 * （COMPONENTS）留在本地：后端启用了本地没有的 id ⇒ 渲染占位页（ModulePlaceholder），
 * 绝不白屏。`/modules` 失败 / 超时 / 结构不对 ⇒ 回退 FALLBACK_SECTIONS 全量导航
 * + 一条可关闭的提示（贴后端原话，不自己编原因）—— 一次后端抖动不许把控制台变成空壳。
 *
 * 一个标签 `/console`，内部若干分区：概览 / 天气 / 升级 / …（由模块清单决定）。
 * 未实现的分区**不渲染**，不摆「敬请期待」占位。
 *
 * 四条交互决定（都在验收里）：
 *   1. 切分区即卸载：只渲染当前分区（key = 分区 id），切走的分区连同它自己的定时器一起卸载
 *      —— 旧行为是「首次进入才挂载、之后一直挂着」，实测逛完 11 个分区 DOM 从 574 涨到 9816
 *      且只增不减。代价：滚动位置不再跨切换保留（本卡明示接受的取舍）。
 *   2. 切分区不重拉：数据走 lib/query.js 的模块级缓存，切回来直接命中。
 *   3. 切作用域只让受影响的分区进 skeleton：缓存 key 里带 agent，没缓存的那个
 *      分区才 loading；顶栏、导航、其它分区不动。导航那一份 key 也带 agent，
 *      所以切作用域会重新拉模块清单并重排（重取期间导航是骨架条，不是空白）。
 *      导航顺序在「首帧」就已经是最终顺序：降级清单的 navOrder 与后端 manifest 逐条相同，
 *      后端清单落地后按同一组 nav_order 排序 ⇒ 不会先画一个顺序再换另一个。
 *   4. 键盘可达：nav 是 <button> 序列，作用域是原生 <select>，Tab 顺序自然成立；
 *      所有可点元素都有 hover + :focus-visible 反馈（见 style.css）。
 *
 * 颜色一律来自 CSS 变量（style.css 里只有 var(--ui-*)），本文件不出现任何字面色值。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { refreshAll, useQuery } from './lib/query.js'
import { useScope } from './lib/scope.js'
import { get } from './lib/rest.js'
import { readPref, writePref } from './lib/storage.js'
import { Empty } from './lib/states.js'
import { OverviewSection } from './sections/overview.js'
import { AgentsSection } from './sections/agents.js'
import { AdvisorSection } from './sections/advisor.js'
import { AutogroupSection } from './sections/autogroup.js'
import { WeatherSection } from './sections/weather.js'
import { UpgradeSection } from './sections/upgrade.js'
import { SettingsSection } from './sections/settings.js'
import { VccSection } from './sections/vcc.js'
import { RemoteSection } from './sections/remote.js'
import { ModelsSection } from './sections/models.js'
import { AutotitlerSection } from './sections/autotitler.js'
import { LanesSection } from './sections/lanes.js'

/**
 * 降级清单：`/modules` 拉不到 / 结构不对 / 超时时的全量导航。**不许删** ——
 * 少了它，一次后端抖动就会变成「一个 tab 都没有」的空壳面板。
 *
 * 顺序 = nav_order，且**必须与 `dashboard/modules/<id>/manifest.json` 里的 nav_order 逐一相同**
 * （overview 10 / weather 20 / models 25 / agents 30 / autogroup 40 / autotitler 45 /
 * advisor 50 / lanes 55 / upgrade 60 / settings 70 / vcc 80 / remote 90）。首帧渲染的是这份清单，
 * `/modules` 落地后渲染的是后端按同一组 nav_order 排出来的清单 —— 两侧顺序一致，
 * 导航才不会有「先按 A 顺序画、800~2000ms 后换成 B 顺序」的重排（实测因此点错分区）。
 * 改这里之前先看一眼各 manifest 的 nav_order；`navOrder` 字段在这里是**唯一排序依据**。
 */
var FALLBACK_SECTIONS = [
  { id: 'overview', label: '概览', icon: 'overview', hint: '系统 / 定时任务 / 会话', navOrder: 10 },
  { id: 'weather', label: '天气', icon: 'weather', hint: '实时 / 逐日 / 逐时', navOrder: 20 },
  // 模型：读每个 agent 自己的 model/provider。切模型是真写 config.yaml 的高危操作，
  // 后端 nav_order=25、默认关（modules/models/manifest.json），启用后才出现在导航里。
  { id: 'models', label: '模型', icon: 'models', hint: '当前模型 / provider / 快照', navOrder: 25 },
  // ignoresScope：本分区永远取全部 agent，顶部作用域选择器对它无效（标题旁写明边界）
  { id: 'agents', label: 'Agent 总览', icon: 'agents', hint: '全部 agent 一屏 / 单行下钻', ignoresScope: true, navOrder: 30 },
  // 项目分组：只读展示 + 手动扫描（写操作留给 C8c）。数据读 Hermes 主目录，与作用域无关。
  { id: 'autogroup', label: '项目分组', icon: 'autogroup', hint: '建议 / 批次 / 规则', ignoresScope: true, navOrder: 40 },
  // 自动命名：hermes-auto-titler 的参数（按 config_schema 动态渲染）+ 运行监控。
  // 监控是所有分区里唯一"读日志"的那个：插件当前 98 次失败 / 2 次改名，必须一眼看见。
  { id: 'autotitler', label: '自动命名', icon: 'autotitler', hint: '参数 / 失败率 / 重试队列', navOrder: 45 },
  // Advisor 审核台：台账按 profile 隔离，与顶部作用域选择器无关。
  { id: 'advisor', label: 'Advisor', icon: 'advisor', hint: '建议 / 决定 / 策略', ignoresScope: true, navOrder: 50 },
  // 执行 Lane：两条外部执行 lane 的心跳。**只读**（没有任何启停按钮：误点会杀 supervisor）。
  { id: 'lanes', label: '执行 Lane', icon: 'vcc', hint: '在线 / 心跳 / 当前卡', ignoresScope: true, navOrder: 55 },
  { id: 'upgrade', label: '升级', icon: 'upgrade', hint: '扫描 / 就绪 / 暂停恢复', navOrder: 60 },
  // 设置：按 agent 的插件三态开关 + 每个插件的配置（来源层 / 脱敏只读 / 乐观锁）。
  // 后端 catalog 字段（ref / module_ref / version…）有就用、缺就降级，见 src/sections/settings.js。
  { id: 'settings', label: '设置', icon: 'settings', hint: '插件 / 配置 / 来源层', navOrder: 70 },
  // 上下文引擎：读每个 agent 自己的 context.engine；唯一写操作（切引擎）要二次确认。
  // 后端 nav_order=80、默认关（modules/vcc/manifest.json），启用后才出现在导航里。
  { id: 'vcc', label: '上下文引擎', icon: 'vcc', hint: '引擎 / 压缩 / 归档', navOrder: 80 },
  // 远端后端：一屏看全所有 agent 的对外 serve 后端；启停是真实进程操作，必须二次确认。
  // 后端 nav_order=90、默认关（modules/remote/manifest.json），启用后才出现在导航里。
  { id: 'remote', label: '远端后端', icon: 'remote', hint: '状态 / 启停 / 凭据', ignoresScope: true, navOrder: 90 }
]
var FALLBACK_ORDER = FALLBACK_SECTIONS.slice().sort(function (a, b) {
  return a.navOrder - b.navOrder
})

/**
 * id → 分区组件。**必须留在本地字面量里**：前端是打包产物，组件只能静态可达
 * （`import(id)` 这类动态加载在桌面端插件加载器里拿不到）。
 */
var COMPONENTS = {
  overview: OverviewSection,
  weather: WeatherSection,
  agents: AgentsSection,
  autogroup: AutogroupSection,
  advisor: AdvisorSection,
  upgrade: UpgradeSection,
  settings: SettingsSection,
  vcc: VccSection,
  remote: RemoteSection,
  models: ModelsSection,
  autotitler: AutotitlerSection,
  lanes: LanesSection
}

/** 视觉元数据（label / icon / hint / ignoresScope）按分区 id 索引；后端 manifest 里没有这些字段。 */
var SECTION_META = {}
FALLBACK_SECTIONS.forEach(function (item) {
  SECTION_META[item.id] = item
})

/** 降级清单的一条 → 导航行。 */
function rowFrom(item) {
  return {
    id: item.id,
    label: item.label,
    icon: item.icon,
    hint: item.hint,
    ignoresScope: Boolean(item.ignoresScope),
    navOrder: item.navOrder,
    Component: COMPONENTS[item.id] || null
  }
}

/** `nav_order` 缺失 / 非数字时排到最后（不用 NaN 让 sort 行为不定）。 */
function navOrder(item) {
  var value = item && (item.nav_order !== undefined ? item.nav_order : item.navOrder)
  return typeof value === 'number' && isFinite(value) ? value : Infinity
}

/**
 * 后端 `/modules` 的一条 → 导航行。
 * 本地没有对应组件也照样出 tab（Component 为 null，面板位置交给占位页）。
 */
function rowFromModule(module) {
  var source = module || {}
  var id = typeof source.id === 'string' ? source.id : ''
  var section = typeof source.section === 'string' && source.section ? source.section : id
  var meta = SECTION_META[section] || SECTION_META[id] || null
  return {
    id: id,
    label: typeof source.label === 'string' && source.label ? source.label : meta ? meta.label : id,
    icon: meta ? meta.icon : '',
    hint: meta ? meta.hint : '模块',
    // manifest 里没有该字段 ⇒ 按现有分区 id 的语义决定（agents / autogroup / advisor 恒取全部 agent）
    ignoresScope: Boolean(
      source.ignoresScope === true || source.ignores_scope === true || (meta && meta.ignoresScope)
    ),
    navOrder: typeof source.nav_order === 'number' && isFinite(source.nav_order) ? source.nav_order : meta ? meta.navOrder : Infinity,
    Component: COMPONENTS[section] || COMPONENTS[id] || null
  }
}

/**
 * `modules` 数组 → 导航行：**只留 enabled === true**、按 `nav_order` 升序。
 * 结构不对（不是数组）返回 null —— 调用方据此走降级路径（不能把结构错当「零个模块」）。
 * 响应体形状：`{ok:true, data:{agent, modules:[…], health:{…}}}`（`data` 是后端包装，
 * 别和 rest.js 的 `{ok,data}` 外壳搞混）。
 */
function rowsFromModules(payload) {
  var body = payload || {}
  var inner = body.data && typeof body.data === 'object' ? body.data : null
  var list = inner && Array.isArray(inner.modules) ? inner.modules : Array.isArray(body.modules) ? body.modules : null
  if (!list) return null
  return list
    .filter(function (module) {
      return module && module.enabled === true
    })
    .sort(function (a, b) {
      return navOrder(a) - navOrder(b)
    })
    .map(rowFromModule)
}

/**
 * `/modules` 的三种结局 → 导航行。**任何一条都不给空面板**：
 *   loading              → 先摆上次成功的行（首次 = 降级清单），补一条骨架条；
 *   ok 且结构对           → 后端启用集（可能就是空数组 —— 那是「都关了」，走空态文案）；
 *   ok 但结构不对 / 失败   → 降级清单全量 + 一条提示（reason 里保留后端原话）。
 */
function resolveNav(status, payload, error) {
  if (status === 'loading') {
    // 切作用域触发重取时保留上一份可用清单：否则导航会先弹回 6 项再缩成 3 项，
    // 视觉上像「跳了一下」。lastRows 只在本模块实例内记账（每次挂载各自一份）。
    return { rows: lastRows || FALLBACK_ORDER.map(rowFrom), loading: true, failure: '', reason: '' }
  }
  var fromBackend = status === 'ok' ? rowsFromModules(payload) : null
  if (fromBackend) {
    lastRows = fromBackend
    return { rows: fromBackend, loading: false, failure: '', reason: '' }
  }
  lastRows = null
  return {
    rows: FALLBACK_ORDER.map(rowFrom),
    loading: false,
    failure: '模块列表拉取失败，已回退到完整导航',
    reason:
      status === 'error'
        ? String(error || '后端没有给出原因')
        : '后端 /modules 返回的结构不对（没有 modules 数组）'
  }
}

/** 上次停在哪个分区（便利项；失效的 id 回落到第一个分区）。 */
var SECTION_KEY = 'nav.section'

/** 上一次成功解析出的导航行（切作用域重取期间先用它撑住，避免导航跳一下）。 */
var lastRows = null

/** 16px 线性图标，只描边不填色（currentColor 跟着文字走）。 */
var ICONS = {
  overview: ['M4 5h6v6H4z', 'M14 5h6v4h-6z', 'M14 13h6v6h-6z', 'M4 15h6v4H4z'],
  agents: ['M9 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M3 20v-1a5 5 0 0 1 5-5h2a5 5 0 0 1 5 5v1', 'M16 4.5a3 3 0 0 1 0 5.8', 'M18 14a5 5 0 0 1 3 4.6V20'],
  weather: ['M12 3v2', 'M12 19v2', 'M3 12h2', 'M19 12h2', 'M5.6 5.6 7 7', 'M17 17l1.4 1.4', 'M18.4 5.6 17 7', 'M7 17l-1.4 1.4', 'M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8z'],
  autogroup: ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z', 'M8 13h8'],
  // 审核台：一枚「勾选在方框里」的记号（建议 → 决定）
  advisor: ['M4 5h9a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z', 'M4 9h9', 'M6 13.5l1.8 1.8L11 12', 'M20 4l-3 3'],
  upgrade: ['M12 16V4', 'M7 9l5-5 5 5', 'M4 20h16'],
  // 设置：齿轮（圆 + 八个齿），与「升级」的箭头上行区分
  settings: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M12 2v3', 'M12 19v3', 'M2 12h3', 'M19 12h3', 'M4.9 4.9 7 7', 'M17 17l2.1 2.1', 'M19.1 4.9 17 7', 'M7 17l-2.1 2.1'],
  // 上下文引擎：三层堆叠（上下文被压成一层摘要），与「设置」的齿轮区分
  vcc: ['M12 3 3 8l9 5 9-5-9-5z', 'M3 12l9 5 9-5', 'M3 16l9 5 9-5'],
  // 远端后端：一座塔 + 两圈信号（对外可连），与「上下文引擎」的堆叠层区分
  remote: ['M12 4v3', 'M9 20h6', 'M10 20l-1-6h6l-1 6', 'M7.5 8.5a6 6 0 0 0 0 5', 'M16.5 8.5a6 6 0 0 1 0 5'],
  // 模型：一颗芯片（方框 + 两侧引脚 + 中心核），与「上下文引擎」的堆叠层区分
  models: ['M7 7h10v10H7z', 'M10 10h4v4h-4z', 'M10 3v4', 'M14 3v4', 'M10 17v4', 'M14 17v4', 'M3 10h4', 'M3 14h4', 'M17 10h4', 'M17 14h4'],
  // 自动命名：一支笔尖 + 一条标题线（"给会话写字"），与「设置」的齿轮区分
  autotitler: ['M4 20l4-1 9.5-9.5a2 2 0 0 0 0-2.8l-0.2-0.2a2 2 0 0 0-2.8 0L5 16l-1 4z', 'M13 7l4 4']
}

function Icon(props) {
  var paths = ICONS[props.name] || []
  return jsx('svg', {
    width: '16',
    height: '16',
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: '1.6',
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': 'true',
    focusable: 'false',
    children: paths.map(function (d, i) {
      return jsx('path', { d: d }, 'p' + i)
    })
  })
}

/**
 * 作用域选择器：原生 select（键盘 / 无障碍直接可用，不自己造下拉）。
 *
 * `na`（not applicable）：当前分区 `ignoresScope === true`，它读的是全部 agent，顶栏这个
 * 选择器对它**完全无效**。这时候必须让它「不像第二个 agent 选择器」——否则同一屏上会出现
 * 两个长得一样、含义不同的下拉（正是「同一个东西让我选两遍」）。做法：整块 `hidden` 且
 * `disabled`（只保留在 DOM 里供无障碍/探针断言，不会被 Tab 到、看不到），由分区标题旁那句
 * 「全部 agent」chip 承接说明。切到受作用域约束的分区时 `na=false`，选择器原样回来。
 */
function ScopeSelect(props) {
  var na = props.na === true
  return jsxs('div', {
    className: 'hc-scope' + (na ? ' hc-scope--na' : ''),
    hidden: na,
    'data-scope-na': na ? 'yes' : 'no',
    children: [
      jsx('label', { className: 'hc-scope__label', htmlFor: 'hc-scope-select', children: '作用域' }),
      jsx('select', {
        id: 'hc-scope-select',
        className: 'hc-select',
        value: props.value,
        hidden: na,
        disabled: na,
        'aria-hidden': na ? 'true' : undefined,
        tabIndex: na ? -1 : undefined,
        onChange: function (event) {
          props.onChange(event.target.value)
        },
        title: na ? '本分区不受作用域约束（始终读全部 agent），该选择器在此隐藏' : props.title,
        children: props.options.map(function (option) {
          return jsx('option', { value: option.id, children: option.label }, option.id || 'current')
        })
      })
    ]
  })
}

/** 连接状态点：形状 + 文字双通道（不靠颜色单独表达）。 */
function Connection(props) {
  var tone = props.status === 'error' ? 'error' : props.status === 'loading' ? 'loading' : 'ok'
  var text = tone === 'error' ? '连接失败' : tone === 'loading' ? '连接中' : '已连接'
  return jsxs('span', {
    className: 'hc-conn hc-conn--' + tone,
    title: props.reason || text,
    children: [
      jsx('span', { className: 'hc-conn__dot', 'aria-hidden': 'true' }),
      jsx('span', { className: 'hc-conn__text', children: text })
    ]
  })
}

/** 导航重取中的骨架条：占住导航的形状，不闪空白（也不写「加载中」三个字）。 */
function NavSkeleton() {
  return jsx('li', {
    className: 'hc-nav__skel',
    'aria-hidden': 'true',
    children: [0, 1].map(function (i) {
      return jsx('span', { className: 'hc-nav__skel-bar' }, 'bar' + i)
    })
  })
}

/**
 * 「后端启用了、前端没打包」的占位分区。不抛错、不白屏：面板里点名是哪个 id，
 * 并说清怎么收场（去模块设置里关掉它，或在前端补上组件）。
 */
function ModulePlaceholder(props) {
  return jsxs('div', {
    className: 'hc-state',
    children: [
      jsx('div', { className: 'hc-state__title', children: props.label || props.id }),
      jsx('div', {
        className: 'hc-state__hint',
        children: '该模块已启用，但前端未打包对应分区（id=' + props.id + '）'
      }),
      jsx('div', {
        className: 'hc-state__code',
        children: '模块 id：' + props.id + ' · 组件映射见 src/shell.js 的 COMPONENTS'
      })
    ]
  })
}

/** 导航降级提示：明说已回退，并把后端原话贴出来（不自己编原因）。可关闭。 */
function NavNotice(props) {
  return jsxs('div', {
    className: 'hc-notice',
    role: 'status',
    children: [
      jsx('span', { className: 'hc-notice__text', children: props.text }),
      jsx('button', {
        type: 'button',
        className: 'hc-notice__close',
        title: '关闭这条提示',
        'aria-label': '关闭提示',
        onClick: props.onClose,
        children: '关闭'
      })
    ]
  })
}

export function ConsoleApp() {
  var scope = useScope()

  // 模块清单：key 带 agent ⇒ 切作用域自动重取并按新启用集重排（不同 agent 启用集不同）
  var modulesQuery = useQuery(
    'modules:' + (scope.value || ''),
    function () {
      return get('/modules', { agent: scope.value })
    },
    { staleMs: 60000 }
  )
  var nav = resolveNav(modulesQuery.status, modulesQuery.data, modulesQuery.error)
  var rows = nav.rows

  var sectionState = useState(readPref(SECTION_KEY, FALLBACK_SECTIONS[0].id))

  var noticeState = useState(false)
  var noticeClosed = noticeState[0]

  var wanted = sectionState[0]
  var active =
    rows.filter(function (item) {
      return item.id === wanted
    })[0] ||
    rows[0] ||
    null

  function selectSection(id) {
    sectionState[1](id)
    writePref(SECTION_KEY, id)
  }

  // 刷新按钮要有可见反馈：数据是「保留旧内容 + 后台换新」，点了如果毫无变化会像没反应。
  // 用 800ms 的短反馈（不是转圈、不锁按钮）；定时器在 effect 里起，卸载时清掉。
  var flashState = useState(false)
  var flash = flashState[0]
  useEffect(
    function () {
      if (!flash) return undefined
      var timer = setTimeout(function () {
        flashState[1](false)
      }, 800)
      return function () {
        clearTimeout(timer)
      }
    },
    [flash]
  )
  function refresh() {
    refreshAll()
    flashState[1](true)
  }

  // 只渲染当前分区：切走即卸载（旧分区留在 DOM 里只增不减 —— 实测 11 个分区逛完 574 → 9816 节点，
  // 且每个分区自己的轮询会继续跑）。数据走 lib/query.js 的模块级缓存，所以切回来命中缓存、不重拉；
  // 代价是各分区的滚动位置不再跨切换保留（切回一个分区从顶部开始），这是本卡明示接受的取舍。
  var sectionNodes = active
    ? [
        jsxs(
          'div',
          {
            className: 'hc-pane',
            children: [
              jsxs('div', {
                className: 'hc-pane__head',
                children: [
                  jsx('span', { className: 'hc-pane__title', children: active.label }),
                  jsx('span', { className: 'hc-chip', children: active.hint }),
                  // 忽略作用域的分区（Agent 总览）不显示「作用域 x」——那会和「本页显示全部」
                  // 自相矛盾。它自己那行「本页显示全部 N 个 agent」就是边界说明。
                  active.ignoresScope
                    ? jsx('span', {
                        className: 'hc-chip',
                        title: '本分区不受顶部作用域选择器影响，始终取全部 agent',
                        children: '全部 agent'
                      })
                    : jsx('span', {
                        className: 'hc-chip',
                        title: '当前作用域（agent）；切换只重拉本分区',
                        children: '作用域 ' + (scope.value || '当前 profile')
                      })
                ]
              }),
              // 本地没打包该 id 时 Component 为 null ⇒ 占位页（而不是空面板 / 抛错）。
              // key = 分区 id：换分区时 React 真的卸载旧的、挂载新的（同 id 重渲染才复用 DOM）。
              jsx(
                active.Component || ModulePlaceholder,
                {
                  id: active.id,
                  label: active.label,
                  agent: scope.value,
                  ready: scope.status !== 'loading',
                  // 「打开该 agent」= 把作用域切到它并回到概览分区（本面板没有 per-agent URL，
                  // 作用域就是这里唯一的“地址”，所以深链就是它）
                  onOpenAgent: function (id) {
                    scope.setValue(id)
                    selectSection('overview')
                  }
                },
                active.id
              )
            ]
          },
          active.id
        )
      ]
    : []

  return jsxs('div', {
    className: 'hc-root',
    children: [
      jsxs('aside', {
        className: 'hc-rail',
        'aria-label': '控制台分区',
        children: [
          jsx('div', { className: 'hc-rail__brand', children: '控制台' }),
          rows.length
            ? jsx('ul', {
                className: 'hc-nav' + (nav.loading ? ' hc-nav--loading' : ''),
                'aria-busy': nav.loading ? 'true' : undefined,
                children: rows
                  .map(function (item) {
                    var isActive = item.id === active.id
                    return jsx(
                      'li',
                      {
                        children: jsxs(
                          'button',
                          {
                            type: 'button',
                            className: 'hc-nav__item' + (isActive ? ' hc-nav__item--active' : ''),
                            'aria-current': isActive ? 'page' : undefined,
                            title: item.label,
                            onClick: function () {
                              selectSection(item.id)
                            },
                            children: [
                              jsx('span', { className: 'hc-nav__icon', children: jsx(Icon, { name: item.icon }) }),
                              jsx('span', { className: 'hc-nav__label', children: item.label })
                            ]
                          },
                          'button'
                        )
                      },
                      item.id
                    )
                  })
                  // 重取中补一条骨架条：导航不闪空白，也不写「加载中…」这种没信息量的字
                  .concat(nav.loading ? [jsx(NavSkeleton, {}, 'skel')] : [])
              })
            : // 后端一个模块都没启用：导航真的空，但必须说清为什么空（不是白屏）
              jsx('div', { className: 'hc-nav__empty', children: '没有已启用模块' }),
          nav.failure && !noticeClosed
            ? jsx(NavNotice, {
                text: nav.failure + '：' + nav.reason,
                onClose: function () {
                  noticeState[1](true)
                }
              })
            : null
        ]
      }),
      jsxs('div', {
        className: 'hc-main',
        children: [
          jsxs('div', {
            className: 'hc-topbar',
            children: [
              jsx('div', { className: 'hc-topbar__title', children: active ? active.label : '控制台' }),
              jsxs('div', {
                className: 'hc-topbar__right',
                children: [
                  jsx(ScopeSelect, {
                    value: scope.value,
                    options: scope.options,
                    // 当前分区不吃作用域（Agent 总览 / 项目分组 / Advisor / 远端后端）时，
                    // 顶栏这个选择器对它无效：隐藏 + 禁用，避免和分区自己的 agent 选择器撞脸。
                    na: Boolean(active && active.ignoresScope),
                    title: scope.reason || '切换作用域（agent）',
                    onChange: scope.setValue
                  }),
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn' + (flash ? ' hc-btn--done' : ''),
                    title: '重新读取已打开分区的数据（保留当前内容，不闪屏）',
                    onClick: refresh,
                    children: flash ? '已刷新' : '刷新'
                  }),
                  jsx(Connection, { status: scope.status, reason: scope.reason })
                ]
              }),
              scope.reason
                ? jsx('div', { className: 'hc-scope__reason hc-scope__reason--warn', children: scope.reason })
                : null
            ]
          }),
          jsx('div', {
            className: 'hc-content',
            children: sectionNodes.length
              ? sectionNodes
              : // 没有启用的模块 ⇒ 也没有分区可挂：给空态（说清 + 下一步），不是空白容器
                jsx('div', {
                  className: 'hc-pane',
                  children: jsx(Empty, {
                    title: '没有已启用模块',
                    hint:
                      '当前作用域（' +
                      (scope.value || '当前 profile') +
                      '）没有启用任何模块。去模块设置里打开至少一个，或点右上角「刷新」重试。'
                  })
                })
          })
        ]
      })
    ]
  })
}

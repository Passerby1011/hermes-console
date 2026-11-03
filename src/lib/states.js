/**
 * hermes-console · 三态组件（Skeleton / Empty / ErrorState）。
 *
 * 规矩（三个分区必须复用，不许各写一套）：
 *   - 加载中 = 骨架屏，占住内容的形状，不白屏、不用转圈盖住整页；
 *   - 空 = 一句「为什么空」+ 一句「下一步做什么」，可以带一个动作按钮；
 *   - 错 = 一句原因（保留后端原话）+ 重试按钮。
 * 任何一态都不出现「加载中…」这类没有信息量的堆叠文字。
 */
import { jsx } from 'react/jsx-runtime'

/**
 * 骨架屏。宽度用百分比，所以窄窗也自然铺满，不需要按容器算像素。
 * @param {{lines?: number, cards?: number, label?: string}} props
 */
export function Skeleton(props) {
  var lines = props.lines === undefined ? 3 : props.lines
  var cards = props.cards === undefined ? 0 : props.cards
  var widths = ['92%', '68%', '84%', '54%', '76%']
  var rows = []
  for (var c = 0; c < cards; c += 1) {
    rows.push(jsx('div', { className: 'hc-skel__card' }, 'c' + c))
  }
  for (var i = 0; i < lines; i += 1) {
    rows.push(
      jsx('div', { className: 'hc-skel__line', style: { width: widths[i % widths.length] } }, 'l' + i)
    )
  }
  return jsx('div', {
    className: 'hc-skel',
    role: 'status',
    'aria-busy': 'true',
    'aria-label': props.label || '正在读取数据',
    children: rows
  })
}

/**
 * 空态：必须在 hint 里说清「为什么空」和「下一步」。
 * @param {{title: string, hint: string, action?: {label: string, onClick: Function}, tone?: string}} props
 */
export function Empty(props) {
  var action = props.action
  var children = [
    jsx('div', { className: 'hc-state__title', children: props.title }, 'title'),
    jsx('div', { className: 'hc-state__hint', children: props.hint }, 'hint')
  ]
  if (action && typeof action.onClick === 'function') {
    children.push(
      jsx(
        'div',
        {
          className: 'hc-state__actions',
          children: jsx('button', {
            type: 'button',
            className: 'hc-btn',
            onClick: action.onClick,
            children: action.label
          })
        },
        'actions'
      )
    )
  }
  return jsx('div', { className: 'hc-state', children: children })
}

/**
 * 错误态：原因原样给出（用户能据此判断是后端没起还是权限问题）+ 重试。
 * @param {{title?: string, message?: string, detail?: string, onRetry?: Function, retryLabel?: string}} props
 */
export function ErrorState(props) {
  var children = [
    jsx('div', { className: 'hc-state__title', children: props.title || '这块内容没读到' }, 'title'),
    jsx('div', { className: 'hc-state__hint', children: props.message || '原因未知' }, 'message')
  ]
  if (props.detail) {
    children.push(jsx('div', { className: 'hc-state__code', children: props.detail }, 'detail'))
  }
  if (typeof props.onRetry === 'function') {
    children.push(
      jsx(
        'div',
        {
          className: 'hc-state__actions',
          children: jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--accent',
            onClick: props.onRetry,
            children: props.retryLabel || '重试'
          })
        },
        'actions'
      )
    )
  }
  return jsx('div', { className: 'hc-state hc-state--error', children: children })
}

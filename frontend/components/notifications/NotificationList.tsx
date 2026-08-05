'use client'

import type { NotificationItem } from '@/lib/notifications'

/**
 * The list of waiting items, rendered the same way wherever it appears.
 *
 * ONE component behind both the bell's dropdown and the full-screen page, so
 * the two cannot drift into showing different things — the same reasoning that
 * put one poller behind the badge and the list it opens. `roomy` is the entire
 * difference between the two surfaces: more padding and a bigger tap target,
 * not a different row.
 *
 * It knows nothing about recordings or questions. Every word on a row comes
 * off the item, which is what makes a second kind of notification a data
 * change rather than an edit here.
 */
export function NotificationList({
  items,
  onSelect,
  roomy = false,
}: {
  items: NotificationItem[]
  onSelect: (item: NotificationItem) => void
  roomy?: boolean
}) {
  if (items.length === 0) {
    return (
      <div className={`text-center text-gray-500 ${roomy ? 'py-16 text-sm' : 'py-8 text-xs'}`}>
        You&apos;re all caught up.
      </div>
    )
  }

  return (
    <ul className="flex flex-col">
      {items.map(item => {
        const Icon = item.icon
        return (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => onSelect(item)}
              className={`w-full text-left flex items-start gap-3 border-b border-white/5 last:border-b-0
                hover:bg-white/5 transition-colors ${roomy ? 'px-5 py-4' : 'px-4 py-3'}`}
            >
              <Icon
                size={roomy ? 18 : 15}
                className="text-primary-400 shrink-0 mt-0.5"
                aria-hidden
              />
              <span className="min-w-0 flex-1">
                <span
                  className={`block font-medium text-white ${roomy ? 'text-sm' : 'text-[13px]'}`}
                >
                  {item.title}
                </span>
                {item.detail && (
                  // `detail` is CONTENT and may be in any language — the
                  // producer's own words or an interview question, in Hebrew
                  // here — so it picks its own direction. The chrome around
                  // it is English.
                  <span
                    dir="auto"
                    className={`block text-gray-400 mt-0.5 ${
                      roomy ? 'text-xs' : 'text-[11px] line-clamp-2'
                    }`}
                  >
                    {item.detail}
                  </span>
                )}
              </span>
              {item.count !== undefined && (
                <span
                  className="shrink-0 min-w-[20px] h-5 px-1.5 rounded-full bg-primary-500/15
                    border border-primary-500/30 text-[11px] font-semibold text-primary-200
                    flex items-center justify-center tabular-nums"
                >
                  {item.count}
                </span>
              )}
            </button>
          </li>
        )
      })}
    </ul>
  )
}

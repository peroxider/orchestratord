import type { HTMLAttributes } from 'react'

export type BadgeTone =
  | 'neutral'
  | 'good'
  | 'warn'
  | 'bad'
  | 'accent'
  | 'purple'

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone
}

export function Badge({ tone = 'neutral', className, ...props }: BadgeProps) {
  const classes = ['ui-badge', `ui-badge--${tone}`, className]
    .filter(Boolean)
    .join(' ')
  return <span className={classes} {...props} />
}

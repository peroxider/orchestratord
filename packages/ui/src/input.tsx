import type { InputHTMLAttributes } from 'react'

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {}

export function Input({ className, ...props }: InputProps) {
  const classes = ['ui-input', className].filter(Boolean).join(' ')
  return <input className={classes} {...props} />
}

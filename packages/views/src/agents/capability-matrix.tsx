import { Badge } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { capabilityLabels } from './capability-labels'

export interface CapabilityMatrixProps {
  capabilities: Record<string, boolean>
}

export function CapabilityMatrix({ capabilities }: CapabilityMatrixProps) {
  const { locale } = useTranslation()
  const labels = capabilityLabels(locale)
  const knownKeys = new Set(labels.map((c) => c.key))
  const extraKeys = Object.keys(capabilities).filter((k) => !knownKeys.has(k))
  const rows = [
    ...labels.map((c) => ({ key: c.key, label: c.label })),
    ...extraKeys.map((k) => ({ key: k, label: k })),
  ]

  return (
    <ul className="capability-matrix">
      {rows.map((row) => {
        const enabled = capabilities[row.key] === true
        return (
          <li key={row.key} className="capability-matrix__row">
            <span className="capability-matrix__label">{row.label}</span>
            <Badge tone={enabled ? 'good' : 'neutral'}>
              {enabled ? 'yes' : 'no'}
            </Badge>
          </li>
        )
      })}
    </ul>
  )
}

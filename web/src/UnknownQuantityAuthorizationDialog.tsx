import { useState } from 'react'

type Props = {
  slotLabel: string
  slotId: string
  material: string
  color: string | null
  preset: string | null
  partName?: string
  requiredWeightG?: number | null
  safetyMarginPercent?: number | null
  pending: boolean
  error?: string | null
  onClose: () => void
  onConfirm: () => void
}

export function UnknownQuantityAuthorizationDialog({
  slotLabel,
  slotId,
  material,
  color,
  preset,
  partName,
  requiredWeightG,
  safetyMarginPercent,
  pending,
  error,
  onClose,
  onConfirm,
}: Props) {
  const [acknowledged, setAcknowledged] = useState(false)

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target && !pending) onClose()
      }}
    >
      <section
        aria-labelledby="unknown-quantity-title"
        aria-modal="true"
        className="fabrication-dialog quantity-authorization-dialog"
        role="dialog"
      >
        <header>
          <div>
            <span className="eyebrow">Unmeasured filament</span>
            <h2 id="unknown-quantity-title">
              Use {slotLabel} without a quantity estimate?
            </h2>
            <p>
              Bambu reports no readable RFID or weight metadata for this loaded slot.
            </p>
          </div>
          <button
            aria-label="Close quantity confirmation"
            className="dialog-close"
            disabled={pending}
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <div className="quantity-authorization-summary">
          <div>
            <span className="section-label">Slot</span>
            <strong>{slotLabel}</strong>
            <small>{slotId}</small>
          </div>
          <div>
            <span className="section-label">Loaded material</span>
            <strong>
              {material}
              {color ? ` · ${color}` : ''}
            </strong>
            <small>{preset ?? 'No installed Studio preset'}</small>
          </div>
          {partName && (
            <div>
              <span className="section-label">Affected part</span>
              <strong>{partName}</strong>
              {requiredWeightG != null && (
                <small>
                  About {requiredWeightG.toFixed(2)} g required
                  {safetyMarginPercent != null
                    ? `, including ${safetyMarginPercent}% margin`
                    : ''}
                </small>
              )}
            </div>
          )}
        </div>

        <div className="part-warning">
          The app cannot verify remaining filament. This authorization is reused across workflows
          until a detectable material or tray identity change. A same-configuration generic spool
          replacement may not be detectable, and multiple workflows may rely on this confirmation.
        </div>

        <label className="checkbox-label quantity-authorization-check">
          <input
            checked={acknowledged}
            disabled={pending}
            onChange={(event) => setAcknowledged(event.target.checked)}
            type="checkbox"
          />
          I checked this physical spool and confirm it has enough filament.
        </label>

        {error && <p className="error-copy">{error}</p>}

        <footer>
          <button className="secondary-action" disabled={pending} onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary-action"
            disabled={!acknowledged || pending}
            onClick={onConfirm}
          >
            {pending
              ? 'Authorizing…'
              : partName
                ? 'Authorize slot and retry assignment'
                : 'Authorize slot'}
          </button>
        </footer>
      </section>
    </div>
  )
}

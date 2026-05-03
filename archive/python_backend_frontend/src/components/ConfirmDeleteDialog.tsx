interface ConfirmDeleteDialogProps {
  open: boolean;
  title: string;
  message: string;
  targetLabel: string;
  targetValue: string;
  phrase: string;
  inputValue: string;
  inputLabel: string;
  cancelLabel: string;
  confirmLabel: string;
  busy?: boolean;
  onInputChange: (value: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDeleteDialog({
  open,
  title,
  message,
  targetLabel,
  targetValue,
  phrase,
  inputValue,
  inputLabel,
  cancelLabel,
  confirmLabel,
  busy = false,
  onInputChange,
  onCancel,
  onConfirm,
}: ConfirmDeleteDialogProps) {
  if (!open) {
    return null;
  }

  const isConfirmed = inputValue.trim().toUpperCase() === phrase;

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-delete-title">
        <div className="modal-header">
          <h3 id="confirm-delete-title">{title}</h3>
        </div>
        <p className="muted modal-copy">{message}</p>
        <div className="detail-block compact-detail">
          <p className="meta-label">{targetLabel}</p>
          <p className="mono">{targetValue}</p>
        </div>
        <label className="form-label">
          <span className="meta-label">{inputLabel}</span>
          <input value={inputValue} onChange={(event) => onInputChange(event.target.value)} autoFocus />
        </label>
        <p className="notice-line subtle">{phrase}</p>
        <div className="modal-actions">
          <button type="button" className="secondary-button" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button type="button" className="secondary-button danger-button" onClick={onConfirm} disabled={!isConfirmed || busy}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

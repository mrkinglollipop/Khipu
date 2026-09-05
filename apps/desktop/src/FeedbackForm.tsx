import { useCallback, useId, useRef, useState, type RefObject } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open as openFileDialog } from "@tauri-apps/plugin-dialog";
import { Loader2, Paperclip, X } from "lucide-react";
import { SUPPORT_EMAIL } from "./Welcome";
import { Dialog } from "./ui";

const ATTACH_MAX_BYTES = 15 * 1024 * 1024;
const ATTACH_MAX_FILES = 10;

type Attachment = {
  path: string;
  name: string;
  size: number | null;
};

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

async function fileSizesAtPaths(paths: string[]): Promise<(number | null)[]> {
  if (paths.length === 0) return [];
  try {
    return await invoke<(number | null)[]>("feedback_file_sizes", { paths });
  } catch {
    return paths.map(() => null);
  }
}

type Props = {
  open: boolean;
  appVersion: string;
  onClose: () => void;
  onSendingChange: (sending: boolean) => void;
  returnFocusRef: RefObject<HTMLButtonElement | null>;
};

export function FeedbackForm({
  open,
  appVersion,
  onClose,
  onSendingChange,
  returnFocusRef,
}: Props) {
  const emailRef = useRef<HTMLInputElement>(null);
  const messageRef = useRef<HTMLTextAreaElement>(null);
  const headingId = useId();

  const [email, setEmail] = useState("");
  const [message, setMessage] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [fieldErrors, setFieldErrors] = useState<{
    email?: string;
    message?: string;
    attachments?: string;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const successRef = useRef(false);

  const attachmentTotal = attachments.reduce((sum, a) => sum + (a.size ?? 0), 0);
  const sizeKnown = attachments.every((a) => a.size != null);
  const overCap = sizeKnown && attachmentTotal > ATTACH_MAX_BYTES;

  const resetForm = useCallback(() => {
    setEmail("");
    setMessage("");
    setAttachments([]);
    setFieldErrors({});
    setFormError(null);
    setSuccess(false);
    successRef.current = false;
  }, []);

  // showModal / close / Escape / backdrop all live in <Dialog> now; this only
  // decides what closing means for the form.
  const closeDialog = useCallback(() => {
    if (sendingRef.current) return;
    resetForm();
    onClose();
    requestAnimationFrame(() => returnFocusRef.current?.focus());
  }, [onClose, resetForm, returnFocusRef]);

  const pickAttachments = async () => {
    setFieldErrors((prev) => ({ ...prev, attachments: undefined }));
    setFormError(null);
    try {
      const selected = await openFileDialog({ multiple: true });
      if (!selected) return;
      const paths = Array.isArray(selected) ? selected : [selected];
      const existing = new Set(attachments.map((a) => a.path));
      const unique = paths.filter(
        (path): path is string =>
          typeof path === "string" && Boolean(path.trim()) && !existing.has(path),
      );
      const room = ATTACH_MAX_FILES - attachments.length;
      if (room <= 0) {
        setFieldErrors((prev) => ({
          ...prev,
          attachments: `At most ${ATTACH_MAX_FILES} files.`,
        }));
        return;
      }
      const clipped = unique.slice(0, room);
      const sizes = await fileSizesAtPaths(clipped);
      const added: Attachment[] = clipped.map((path, i) => ({
        path,
        name: path.split(/[/\\]/).pop() ?? path,
        size: sizes[i] ?? null,
      }));
      if (added.length) setAttachments((prev) => [...prev, ...added]);
    } catch (e) {
      setFormError(String(e));
    }
  };

  const removeAttachment = (path: string) => {
    setAttachments((prev) => prev.filter((a) => a.path !== path));
    setFieldErrors((prev) => ({ ...prev, attachments: undefined }));
  };

  const validate = (): boolean => {
    const next: typeof fieldErrors = {};
    const trimmedEmail = email.trim();
    const trimmedMessage = message.trim();
    if (!trimmedEmail || !trimmedEmail.includes("@")) {
      next.email = "Enter a valid reply-to email.";
    }
    if (!trimmedMessage) {
      next.message = "Message is required.";
    }
    if (overCap) {
      next.attachments = `Attachments must total ${formatBytes(ATTACH_MAX_BYTES)} or less.`;
    }
    setFieldErrors(next);
    if (next.email) {
      emailRef.current?.focus();
      return false;
    }
    if (next.message) {
      messageRef.current?.focus();
      return false;
    }
    if (next.attachments) return false;
    return true;
  };

  const submit = async () => {
    if (sendingRef.current || successRef.current) return;
    sendingRef.current = true;
    setFormError(null);
    if (!validate()) {
      sendingRef.current = false;
      return;
    }
    setSending(true);
    onSendingChange(true);
    try {
      await invoke("send_feedback", {
        replyTo: email.trim(),
        message: message.trim(),
        appVersion,
        paths: attachments.map((a) => a.path),
      });
      successRef.current = true;
      setSuccess(true);
    } catch (e) {
      setFormError(String(e));
    } finally {
      sendingRef.current = false;
      setSending(false);
      onSendingChange(false);
    }
  };

  const onFormKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      if (sendingRef.current || successRef.current) return;
      void submit();
    }
  };

  return (
    <Dialog
      open={open}
      className="feedback-dialog"
      ariaLabelledBy={headingId}
      initialFocusRef={emailRef}
      onCancel={closeDialog}
      onClose={closeDialog}
    >
      <form
        className="feedback-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (sendingRef.current || successRef.current) return;
          void submit();
        }}
        onKeyDown={onFormKeyDown}
        noValidate
      >
        <header className="feedback-head">
          <h2 id={headingId}>Send feedback</h2>
          <button
            type="button"
            className="feedback-close"
            aria-label="Close"
            onClick={closeDialog}
            disabled={sending}
          >
            <X size={16} aria-hidden />
          </button>
        </header>

        {success ? (
          <div className="feedback-success" role="status">
            <p className="feedback-success-title">Feedback sent</p>
            <p className="muted">
              Thanks — your message was emailed to {SUPPORT_EMAIL}. We&apos;ll reply to the
              address you provided.
            </p>
            <div className="feedback-actions">
              <button type="button" className="primary" onClick={closeDialog}>
                Close
              </button>
            </div>
          </div>
        ) : (
          <>
            <p className="muted feedback-lede">
              Emails <strong>{SUPPORT_EMAIL}</strong>. Optional files, 15 megabytes total.
              Nothing Khipu has recorded is included unless you attach a file.
            </p>

            <label className="feedback-field">
              <span className="feedback-label">Reply-to email</span>
              <input
                ref={emailRef}
                type="email"
                name="reply_to"
                autoComplete="email"
                spellCheck={false}
                required
                value={email}
                disabled={sending}
                aria-invalid={fieldErrors.email ? true : undefined}
                aria-describedby={fieldErrors.email ? "feedback-email-err" : undefined}
                onChange={(e) => {
                  setEmail(e.target.value);
                  setFieldErrors((prev) => ({ ...prev, email: undefined }));
                }}
              />
              {fieldErrors.email ? (
                <span id="feedback-email-err" className="feedback-field-err" role="alert">
                  {fieldErrors.email}
                </span>
              ) : null}
            </label>

            <label className="feedback-field">
              <span className="feedback-label">Message</span>
              <textarea
                ref={messageRef}
                className="feedback-message"
                name="message"
                required
                rows={6}
                value={message}
                disabled={sending}
                aria-invalid={fieldErrors.message ? true : undefined}
                aria-describedby={fieldErrors.message ? "feedback-message-err" : undefined}
                onChange={(e) => {
                  setMessage(e.target.value);
                  setFieldErrors((prev) => ({ ...prev, message: undefined }));
                }}
              />
              {fieldErrors.message ? (
                <span id="feedback-message-err" className="feedback-field-err" role="alert">
                  {fieldErrors.message}
                </span>
              ) : null}
              <span className="feedback-hint muted">⌘/Ctrl+Enter to send</span>
            </label>

            <div className="feedback-attach-block">
              <div className="feedback-attach-toolbar">
                <button
                  type="button"
                  disabled={sending}
                  onClick={() => void pickAttachments()}
                >
                  <Paperclip size={14} aria-hidden />
                  Attach files
                </button>
                <span className="muted feedback-attach-total">
                  {attachments.length === 0
                    ? `0 / ${formatBytes(ATTACH_MAX_BYTES)}`
                    : `${formatBytes(attachmentTotal)} / ${formatBytes(ATTACH_MAX_BYTES)}${
                        !sizeKnown ? " (size pending…)" : overCap ? " — over limit" : ""
                      }`}
                </span>
              </div>
              {fieldErrors.attachments ? (
                <span className="feedback-field-err" role="alert">
                  {fieldErrors.attachments}
                </span>
              ) : null}
              {attachments.length > 0 ? (
                <ul className="feedback-attach-list">
                  {attachments.map((a) => (
                    <li key={a.path}>
                      <span className="feedback-attach-name" title={a.path}>
                        {a.name}
                      </span>
                      <span className="feedback-attach-size muted">
                        {a.size == null ? "…" : formatBytes(a.size)}
                      </span>
                      <button
                        type="button"
                        className="feedback-attach-remove"
                        aria-label={`Remove ${a.name}`}
                        disabled={sending}
                        onClick={() => removeAttachment(a.path)}
                      >
                        <X size={12} aria-hidden />
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>

            {formError ? (
              <div className="feedback-form-err" role="alert">
                {formError}
              </div>
            ) : null}

            <div className="feedback-actions">
              <button type="button" onClick={closeDialog} disabled={sending}>
                Cancel
              </button>
              <button type="submit" className="primary" disabled={sending}>
                {sending ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                Send
              </button>
            </div>
          </>
        )}
      </form>
    </Dialog>
  );
}

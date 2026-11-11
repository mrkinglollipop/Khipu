// A bare answer for a command whose real reply is not JSON text (e.g. `true`).
export const RAW = Symbol("raw");
export type Raw = { [RAW]: unknown };
export const raw = (value: unknown): Raw => ({ [RAW]: value });
export const isRaw = (v: unknown): v is Raw => typeof v === "object" && v !== null && RAW in v;

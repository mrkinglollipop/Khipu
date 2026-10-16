// The native file and folder pickers, faked for the dev preview: a fixed,
// fictional path after a short wait.
export async function open(opts?: { directory?: boolean }): Promise<string | null> {
  await new Promise((r) => setTimeout(r, 150));
  return opts?.directory ? "/home/someone/.nvm/versions/node/v20.11.0/bin" : "/opt/fake/bin/codex";
}

export async function save(): Promise<string | null> {
  return null;
}

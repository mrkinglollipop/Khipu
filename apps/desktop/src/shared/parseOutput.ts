/** Read the JSON a `khipu` command printed. Some commands (search) print one
 *  indented document over many lines; others print log lines and then one JSON
 *  line. Try the whole trimmed output first, then the last line alone. Returns
 *  undefined when neither reads. */
export function parseCommandJson(raw: string): unknown {
  const text = raw.trim();
  try {
    return JSON.parse(text);
  } catch {
    try {
      return JSON.parse(text.split("\n").pop() ?? "");
    } catch {
      return undefined;
    }
  }
}

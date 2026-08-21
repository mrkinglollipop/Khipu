# Why source — git / gh

Always run.

## Search

- `git blame -L` on anchored lines
- `git log --follow -p -- <file>` (bounded)
- `git log --oneline -20 -- <file>`
- Extract PR numbers from subjects; `gh pr view <n> --json title,body,author,createdAt,mergedAt,labels,closingIssuesReferences,comments,reviews`
- Linked issues via PR closing references

## What this category uniquely surfaces

Implementation-time rationale: PR problem statements, review debates, inline comments, test names encoding edge cases, commit messages linking tickets.

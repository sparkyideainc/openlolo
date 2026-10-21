# Documentation

Everything in this folder is published at [openlolo.com/docs](https://openlolo.com/docs). The
site renders it with [Fumadocs](https://fumadocs.dev), and a push to `main` that changes this
folder redeploys it. Engineering notes and plans that are not published live in
[`design/`](../design/README.md).

- One `.mdx` file per page, with `title` and `description` in the frontmatter. Other files,
  such as this README, are not published.
- `meta.json` in each folder sets the sidebar title and page order.
- Link between pages with `/docs/<folder>/<page>`.
- Available components: `<Callout>` (`type="warn"` for warnings), `<Cards>` / `<Card>`,
  `<Tabs>` / `<Tab>`.
- Use `{/* TODO: ... */}` for unfinished parts; it does not render.
- Describe only behaviour that has shipped. Planned behaviour stays in `design/plans/`.

Contributions follow [CONTRIBUTING.md](../CONTRIBUTING.md), including the vouch step before a
first pull request.

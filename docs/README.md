# Documentation

Everything in this folder is published at [openlolo.com/docs](https://openlolo.com/docs). The
site renders it with [Fumadocs](https://fumadocs.dev), and a push to `main` that changes this
folder redeploys it. Engineering notes and plans that are not published live in
[`design/`](../design/README.md).

- One `.mdx` file per page, with `title` and `description` in the frontmatter. Other files,
  such as this README, are not published.
- `meta.json` in each folder sets the sidebar title and page order.
- Link between pages with `/docs/<folder>/<page>`.
- Available components: `<Callout>` (`type="warn"` for warnings, `type="success"` for a
  checkpoint), `<Steps>` / `<Step>`, `<Cards>` / `<Card>`, `<Tabs>` / `<Tab>`. Images zoom on click.
- `<ShopProducts type="Hardware" />` lists the shop's products of that Shopify product type,
  with prices, or links to the shop when there are none.
- `parts` in the frontmatter (a list of `name`, optional shop `handle`) shows a "You'll need"
  card beside the page.
- The journey is three numbered steps, one root folder each, with the number in the folder
  title: `1. Get your LoloBox` (buy one, or build one yourself, which also holds installation),
  `2. Pair your LoloBox` and `3. Connect your first AI`. The sidebar shows the number as a
  badge. Optional remote access guides live under `Customize your LoloBox`; troubleshooting and
  reference pages live under `Help`.
- Name each folder and file after its title in lowercase kebab-case, without the step number:
  `1. Get your LoloBox` is `get-your-lolobox/`, "Run the install command" is
  `run-the-install-command.mdx`. A folder's own page is `index.mdx`. Rename the file when its
  title changes, and update the links to it. Renaming changes the page's URL, so once the
  guide is published, change the titles of pages that others link to only when needed.
- Ready-made boxes skip build and installation. Both paths join at `/docs/pair-your-lolobox`.
- Use everyday language in navigation. Explain terms such as pairing and MCP where needed.
- End journey pages with a clear next step. Match setup order to the shipped portal: cable,
  Trust, Developer Mode, then Wi-Fi.
- Use `{/* TODO: ... */}` for unfinished parts; it does not render.
- Describe only behaviour that has shipped. Planned behaviour stays in `design/plans/`.

Contributions follow [CONTRIBUTING.md](../CONTRIBUTING.md), including the vouch step before a
first pull request.

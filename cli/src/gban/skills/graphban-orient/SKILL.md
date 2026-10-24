---
name: graphban-orient
description: >
  Read a Graphban-tracked repo through the code graph before grep or a wide
  file read. Use when starting work, finding where code lives, or before
  search_code, code_neighbors, get_code_map, search_memory, or a filesystem
  crawl. Use when the user runs /graphban-orient.
user-invocable: true
---

# Orient

Ask the graph before you read the tree. One graph call replaces a crawl.

1. `get_context` once per session. Keep the project id, `missing_tiers`, and gitops. A later call does not refresh a manifest that was fetched at connect.
2. Unset gitops (`source` unmeasured, or `control` `linked_unreachable`) is unmeasured. Leave the base branch, the branch pattern, and the release doc alone until a field has a source.
3. `search_code(query, top_k=5)` before grep.
4. `code_neighbors(path)` before opening a file to learn what calls it or what it calls.
5. `search_memory(query, top_k=3)` once per task. Leave `include_candidates` off. A candidate is not a published fact. Writing a shard is `graphban-work`.
6. `related_work(id)` once you have an item id.
7. Open a file when you have a path and a question.

List reads stay on the default `fields=lean`. `search_items` and `get_backlog` return id, title, and status. The one full item read is `get_item_details`, for the item you took. Claiming is `graphban-work`.

`get_code_map` takes a `kind` and a small `limit`. With neither, the reply is up to 200 nodes. Check `truncated` when you do call it.

Looking around uses the reads above. `describe_code`, `link_code`, and `unlink_code` write the graph.

A spec question is `graphban-prd`. `get_prd` is for editing a spec, not for finding a file.

`missing_tiers` on `get_context` means those tools were left out of the prompt. The tier decides what is listed. Scope and role decide what a call may do.

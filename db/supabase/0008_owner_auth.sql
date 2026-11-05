-- 0008_owner_auth.sql
--
-- The board stops being a public page with a shared secret and becomes a
-- private account: one email, a magic-link session, and the whole dataset
-- visible only to that identity.
--
-- Two holes this also closes, both real:
--   * every RPC was executable by `anon`, including claim_slice(), ingest_batch()
--     and prune_snapshots() — the write token guarded the UI helpers but not the
--     crawler's own machinery
--   * public.usage_today was a definer view, so it answered any caller even
--     though `runs` itself is protected by RLS
--
-- The crawler is unaffected: it connects with the service key, which bypasses
-- RLS by design and is never shipped to a browser.

-- ───────────────────────────── who the owner is ──────────────────────────────
insert into public.settings (key, value)
values ('owner', jsonb_build_object(
    'email', 'pityailon@gmail.com',
    'uid',   'e4d86c7a-dcad-4e80-b4c4-101d09845916'))
on conflict (key) do update set value = excluded.value;

-- SECURITY DEFINER so a policy can call it without granting select on settings.
create or replace function public.is_owner()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(
           (select value->>'uid' = coalesce(auth.uid()::text, '')
              from public.settings where key = 'owner'),
           false)
      -- the crawler is the owner's own machinery; it connects with the service key
      or coalesce(current_setting('request.jwt.claims', true)::jsonb->>'role' = 'service_role', false);
$$;

revoke all on function public.is_owner() from public, anon;
-- RLS policies execute as the querying role, so signed-in callers must be able
-- to run it; anon must not (that is what the 403 was).
grant execute on function public.is_owner() to authenticated;

-- ──────────────── one question every write path already asks ─────────────────
-- Existing RPCs call `if not check_token(p_token) then raise`. Rather than
-- rewrite fourteen bodies, widen the question they ask: an owner session, the
-- service role, or the legacy write token all count as "may write".
create or replace function public.check_token(p_token text)
returns boolean
language sql
stable
security definer
set search_path = public, extensions
as $$
  select public.is_owner()
      or coalesce(current_setting('request.jwt.claims', true)::jsonb->>'role' = 'service_role', false)
      or coalesce(
           (select value->>'sha256' = encode(extensions.digest(coalesce(p_token, ''), 'sha256'), 'hex')
              from public.settings where key = 'write_token'),
           false);
$$;

-- ─────────────────────────────── read access ─────────────────────────────────
drop policy if exists read_listings  on public.listings;
drop policy if exists read_snapshots on public.snapshots;
drop policy if exists read_keywords  on public.keywords;
drop policy if exists read_lk        on public.listing_keywords;
drop policy if exists read_runs      on public.runs;

create policy owner_read_listings  on public.listings         for select to authenticated using (public.is_owner());
create policy owner_read_snapshots on public.snapshots        for select to authenticated using (public.is_owner());
create policy owner_read_keywords  on public.keywords         for select to authenticated using (public.is_owner());
create policy owner_read_lk        on public.listing_keywords for select to authenticated using (public.is_owner());
create policy owner_read_runs      on public.runs             for select to authenticated using (public.is_owner());

revoke select on public.listings, public.snapshots, public.keywords,
                 public.listing_keywords, public.runs, public.settings from anon;
grant  select on public.listings, public.snapshots, public.keywords,
                 public.listing_keywords, public.runs to authenticated;

-- the budget lives in settings, which the owner cannot read directly (that table
-- also holds the legacy write-token hash); expose the one number instead
create or replace function public.page_budget()
returns int language sql stable security definer set search_path = public as $$
  select coalesce((select (value->>'max_pages_per_day')::int from public.settings where key = 'limits'), 24);
$$;
revoke execute on function public.page_budget() from public, anon;
grant  execute on function public.page_budget() to authenticated;

-- ─────────────────────── the view that outlived its RLS ──────────────────────
-- security_invoker makes the view obey the caller's policies; the is_owner()
-- term covers the settings row it reads for the budget.
create or replace view public.usage_today with (security_invoker = on) as
select
    coalesce(sum(greatest(0, coalesce(r.page_to, r.page_from)
                          - coalesce(r.page_from, 1) + 1)), 0)::int as pages_used,
    public.page_budget()                                      as pages_budget,
    count(*) filter (where r.status = 'succeeded')            as slices_ok,
    count(*) filter (where r.status = 'aborted')              as slices_blocked,
    count(*)                                                   as slices,
    max(r.finished_at)                                         as last_finished
from public.runs r
where r.started_at >= date_trunc('day', now())
  and public.is_owner();

revoke all on public.usage_today from public, anon;
grant select on public.usage_today to authenticated;

-- ──────────────────────────── execute privileges ─────────────────────────────
-- UI writes: signed-in owner only.
revoke execute on function public.add_keyword(text, text, int, int, int)          from public, anon;
revoke execute on function public.delete_keyword(text, text)                      from public, anon;
revoke execute on function public.set_keyword_status(text, text, text)            from public, anon;
revoke execute on function public.update_keyword(text, text, int, int, int)       from public, anon;
revoke execute on function public.set_interval_bulk(text, text[], int)            from public, anon;
revoke execute on function public.set_page_budget(text, int)                      from public, anon;
revoke execute on function public.set_star(text, text, boolean)                   from public, anon;

-- Crawler machinery: service role only. Nothing in a browser should be able to
-- claim a slice, write rows, or prune history.
revoke execute on function public.claim_slice(int)                                from public, anon, authenticated;
grant  execute on function public.claim_slice(int)                                to service_role;
revoke execute on function public.finish_slice(text, int, text, int)              from public, anon, authenticated;
grant  execute on function public.finish_slice(text, int, text, int)              to service_role;
revoke execute on function public.ingest_batch(text, jsonb)                       from public, anon, authenticated;
grant  execute on function public.ingest_batch(text, jsonb)                       to service_role;
revoke execute on function public.peek_due()                                      from public, anon, authenticated;
grant  execute on function public.peek_due()                                      to service_role;
revoke execute on function public.prune_snapshots(int)                            from public, anon, authenticated;
grant  execute on function public.prune_snapshots(int)                            to service_role;
revoke execute on function public.check_token(text)                               from public, anon, authenticated;

-- Used by the sign-in dialog to tell "typo" from "rotated token".
revoke execute on function public.verify_token(text)                              from public, anon;
grant  execute on function public.verify_token(text)                              to authenticated;

comment on function public.is_owner() is
  'True only for the single owner identity in settings.owner. Gates every table read.';

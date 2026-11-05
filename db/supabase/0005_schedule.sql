-- 0005_schedule.sql
--
-- Per-keyword cadence, so the board can promise "check tshirt every hour" separately
-- from "check ceramic mug once a day" instead of everything sharing one global cron.
--
-- Two columns do the work:
--   interval_hours  how long to wait between slices for this keyword (1–24)
--   due_at          the wall-clock moment the keyword becomes claimable again
--
-- claim_slice() stops picking "the oldest keyword" and starts picking "the keyword
-- that is due, earliest first". A keyword that is not due is simply invisible to the
-- crawler, which is what makes an every-hour keyword and an every-day keyword share
-- one queue without one starving the other.

-- ───────────────────────────── columns ───────────────────────────────────────
alter table public.keywords
    add column if not exists interval_hours int not null default 2,
    add column if not exists due_at         timestamptz not null default now();

alter table public.keywords drop constraint if exists keywords_interval_hours_check;
alter table public.keywords
    add constraint keywords_interval_hours_check check (interval_hours between 1 and 24);

-- claim looks up "due, in due order" on every tick — make that cheap
create index if not exists keywords_due_idx
    on public.keywords (status, due_at, priority desc);

comment on column public.keywords.interval_hours is
    'Hours between slices for this keyword (1-24). The UI calls it "check every N hours".';
comment on column public.keywords.due_at is
    'When this keyword may next be claimed. Set on claim and again on finish, so a '
    'crashed runner cannot hot-loop the same keyword.';

-- ───────────────────────────── claim ─────────────────────────────────────────
-- Return type is unchanged, so this can be replaced in place.
create or replace function public.claim_slice(p_pages int default null)
returns table (keyword text, page_from int, page_to int, depth int, pages int)
language plpgsql
security definer
set search_path = public
as $$
declare
    k      public.keywords;
    pages  int;
    budget int;
    used   int;
begin
    select coalesce((value ->> 'max_pages_per_day')::int, 24)
      into budget
      from public.settings where key = 'limits';
    budget := coalesce(budget, 24);

    select coalesce(sum(greatest(0, coalesce(r.page_to, r.page_from)
                                   - coalesce(r.page_from, 1) + 1)), 0)
      into used
      from public.runs r
     where r.started_at >= date_trunc('day', now());

    if used >= budget then
        raise notice 'daily page budget spent: % of %', used, budget;
        return;
    end if;

    select * into k
      from public.keywords
     where status in ('queued', 'running')
       and due_at <= now()                       -- not due yet = not our problem
     order by due_at asc, priority desc, id asc   -- most overdue first
     limit 1
     for update skip locked;

    if not found then
        return;                                   -- nothing is due right now
    end if;

    -- a finished pass rolls over to page 1 of the next pass
    if k.next_page > k.depth then
        update public.keywords
           set pass_no = k.pass_no + 1, next_page = 1
         where id = k.id
        returning * into k;
    end if;

    pages := greatest(1, least(coalesce(p_pages, k.pages_per_run),
                              k.depth - k.next_page + 1,
                              budget - used));

    update public.keywords
       set status      = 'running',
           last_run_at = now(),
           -- Push the due time forward now, not only on finish: if this runner is
           -- killed mid-crawl, finish_slice() never runs and the keyword would
           -- otherwise be claimable the very next tick, every tick, forever.
           due_at      = now() + make_interval(hours => k.interval_hours)
     where id = k.id;

    return query select k.keyword, k.next_page, k.next_page + pages - 1, k.depth, pages;
end;
$$;

-- ───────────────────────────── finish ────────────────────────────────────────
create or replace function public.finish_slice(
    p_keyword   text,
    p_next_page int,
    p_status    text default null,
    p_listings  int default 0
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.keywords
       set next_page     = greatest(1, p_next_page),
           status        = coalesce(p_status, 'queued'),
           -- the next check is one full interval after this one *finished*
           due_at        = now() + make_interval(hours => interval_hours),
           finished_at   = now(),
           listings_seen = listings_seen + greatest(0, p_listings),
           pages_done    = pages_done + 1
     where keyword = p_keyword;
end;
$$;

revoke all on function public.finish_slice(text, int, text, int) from anon, authenticated;

-- ───────────────────────────── editing cadence ───────────────────────────────
-- One RPC for everything the queue table can edit. NULL means "leave it alone".
create or replace function public.update_keyword(
    p_token          text,
    p_keyword        text,
    p_interval_hours int  default null,
    p_depth          int  default null,
    p_pages_per_run  int  default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
    v_kw  text := lower(btrim(regexp_replace(coalesce(p_keyword, ''), '\s+', ' ', 'g')));
    v_int int  := case when p_interval_hours is null then null
                       else greatest(1, least(24, p_interval_hours)) end;
    v_row public.keywords;
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;

    update public.keywords
       set interval_hours = coalesce(v_int, interval_hours),
           depth          = coalesce(greatest(1, least(100, p_depth)), depth),
           pages_per_run  = coalesce(greatest(1, least(5, p_pages_per_run)), pages_per_run),
           -- Changing the cadence must take effect now, not after the old waiting
           -- period expires. Shortening 24h → 1h would otherwise still wait a day.
           due_at         = case when v_int is null then due_at
                                 else least(due_at, now() + make_interval(hours => v_int)) end
     where keyword = v_kw
    returning * into v_row;

    if not found then
        raise exception 'no such keyword: %', v_kw;
    end if;

    return jsonb_build_object('ok', true, 'keyword', v_row.keyword,
                              'interval_hours', v_row.interval_hours,
                              'depth', v_row.depth, 'due_at', v_row.due_at);
end;
$$;

-- Bulk edit: "the keywords I ticked should all run every 3 hours".
create or replace function public.set_interval_bulk(
    p_token          text,
    p_keywords       text[],
    p_interval_hours int
) returns int
language plpgsql security definer set search_path = public as $$
declare
    v_int   int := greatest(1, least(24, p_interval_hours));
    v_names text[];
    v_rows  int;
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;

    select array_agg(lower(btrim(x))) into v_names
      from unnest(p_keywords) x where length(btrim(x)) > 0;

    if v_names is null or array_length(v_names, 1) = 0 then
        return 0;
    end if;

    update public.keywords
       set interval_hours = v_int,
           status         = case when status = 'blocked' then 'queued' else status end,
           due_at         = least(due_at, now() + make_interval(hours => v_int))
     where keyword = any(v_names);

    get diagnostics v_rows = row_count;
    return v_rows;
end;
$$;

-- ───────────────────────────── politeness dial ───────────────────────────────
-- The daily page budget is the one number that decides how many slices actually
-- happen, so it belongs on the board rather than in a shell command.
create or replace function public.set_page_budget(p_token text, p_pages_per_day int)
returns int
language plpgsql security definer set search_path = public as $$
declare v_int int := greatest(6, least(200, p_pages_per_day));
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    update public.settings
       set value = jsonb_set(coalesce(value, '{}'::jsonb), '{max_pages_per_day}', to_jsonb(v_int))
     where key = 'limits';
    return v_int;
end;
$$;

-- ───────────────────────────── add keyword (with cadence) ────────────────────
-- The 4-argument version has to go: leaving both alive would make PostgREST's
-- named-argument call ambiguous, and every add would fail with "not unique".
drop function if exists public.add_keyword(text, text, int, int);
create function public.add_keyword(
    p_token          text,
    p_keyword        text,
    p_depth          int default 20,
    p_pages_per_run  int default 2,
    p_interval_hours int default 2
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_kw text := lower(btrim(regexp_replace(coalesce(p_keyword, ''), '\s+', ' ', 'g')));
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    if length(v_kw) < 2 or length(v_kw) > 80 then
        raise exception 'keyword must be 2-80 characters';
    end if;
    insert into public.keywords as k (keyword, depth, pages_per_run, interval_hours, status, added_by, due_at)
    values (v_kw, greatest(1, least(100, p_depth)), greatest(1, least(5, p_pages_per_run)),
            greatest(1, least(24, p_interval_hours)), 'queued', 'web', now())
    on conflict (keyword) do update set
        status         = case when k.status in ('paused', 'blocked') then 'queued' else k.status end,
        depth          = greatest(k.depth, excluded.depth),
        interval_hours = excluded.interval_hours,
        due_at         = least(k.due_at, now());
    return jsonb_build_object('ok', true, 'keyword', v_kw);
end;
$$;

revoke all on function public.update_keyword(text, text, int, int, int) from anon, authenticated;
revoke all on function public.set_interval_bulk(text, text[], int) from anon, authenticated;
revoke all on function public.set_page_budget(text, int) from anon, authenticated;
revoke all on function public.add_keyword(text, text, int, int, int) from anon, authenticated;

grant execute on function public.update_keyword(text, text, int, int, int) to authenticated, anon;
grant execute on function public.set_interval_bulk(text, text[], int) to authenticated, anon;
grant execute on function public.set_page_budget(text, int) to authenticated, anon;
grant execute on function public.add_keyword(text, text, int, int, int) to authenticated, anon;

-- ───────────────────────────── what the board reads ──────────────────────────
-- "Am I keeping up?" answered in one row instead of three aggregate queries.
create or replace view public.usage_today as
select
    coalesce(sum(greatest(0, coalesce(r.page_to, r.page_from)
                          - coalesce(r.page_from, 1) + 1)), 0)::int as pages_used,
    (select coalesce((value ->> 'max_pages_per_day')::int, 24)
       from public.settings where key = 'limits')              as pages_budget,
    count(*) filter (where r.status = 'succeeded')             as slices_ok,
    count(*) filter (where r.status = 'aborted')               as slices_blocked,
    count(*)                                                   as slices,
    max(r.finished_at)                                         as last_finished
from public.runs r
where r.started_at >= date_trunc('day', now());

grant select on public.usage_today to anon, authenticated;

-- What the pre-flight job asks before spinning up a browser.
create or replace function public.peek_due()
returns table (keyword text, next_page int, depth int, interval_hours int, due_at timestamptz)
language sql security definer set search_path = public as $$
    select k.keyword, k.next_page, k.depth, k.interval_hours, k.due_at
      from public.keywords k
     where k.status in ('queued', 'running')
       and k.due_at <= now()
     order by k.due_at asc, k.priority desc, k.id asc
     limit 5;
$$;

revoke all on function public.peek_due() from anon, authenticated;

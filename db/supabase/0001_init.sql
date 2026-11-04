-- Etsy research store — Supabase (Postgres) schema.
-- Replaces the local SQLite store as the system of record; the SQLite dashboard
-- keeps working for offline analysis, but GitHub Actions now writes here.
--
-- Design notes
--  * The keyword queue lives in this database, not in the repository. A public
--    repo no longer has to carry an encrypted queue file, and the web UI can
--    add work without a Git commit or a workflow input.
--  * `listings` holds current state *and* the previous observation, so the UI
--    gets favourites/price deltas without a window function over `snapshots`.
--  * `snapshots` is append-only history, deduped per (etsy_id, run_id) and
--    pruned by prune_snapshots() to keep the free tier (500 MB) comfortable.

create extension if not exists pgcrypto with schema extensions;

-- ─────────────────────────────────── queue ───────────────────────────────────
create table if not exists public.keywords (
    id            bigserial primary key,
    keyword       text        not null unique,
    depth         int         not null default 20 check (depth between 1 and 100),
    pages_per_run int         not null default 2  check (pages_per_run between 1 and 5),
    next_page     int         not null default 1,
    pass_no       int         not null default 1,
    status        text        not null default 'queued'
                              check (status in ('queued','running','done','paused','blocked')),
    priority      int         not null default 0,
    added_by      text,
    added_at      timestamptz not null default now(),
    last_run_at   timestamptz,
    finished_at   timestamptz,
    pages_done    int         not null default 0,
    listings_seen int         not null default 0
);
create index if not exists keywords_pick_idx on public.keywords (status, priority desc, last_run_at);

-- ────────────────────────────────── listings ─────────────────────────────────
create table if not exists public.listings (
    etsy_id        text primary key,
    url            text not null,
    title          text,
    shop_name      text,
    shop_url       text,
    price_usd      numeric(10,2),
    prev_price     numeric(10,2),
    currency       text default 'USD',
    favorites      int,
    prev_favorites int,
    reviews        int,
    rating         numeric(3,2),
    image_url      text,
    category_path  text,
    description    text,
    is_bestseller  boolean,
    is_star_seller boolean,
    free_shipping  boolean,
    first_keyword  text,
    first_seen     timestamptz not null default now(),
    last_seen      timestamptz not null default now(),
    obs_count      int not null default 1,
    starred        boolean not null default false,
    search_tsv     tsvector generated always as (
                     setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                     setweight(to_tsvector('english', left(coalesce(description, ''), 8000)), 'B')
                   ) stored
);
create index if not exists listings_search_idx   on public.listings using gin (search_tsv);
create index if not exists listings_fav_idx      on public.listings (favorites desc nulls last);
create index if not exists listings_seen_idx     on public.listings (last_seen desc);
create index if not exists listings_price_idx    on public.listings (price_usd);
create index if not exists listings_shop_idx     on public.listings (shop_name);

-- ───────────────────────────────── snapshots ─────────────────────────────────
create table if not exists public.snapshots (
    id          bigserial primary key,
    etsy_id     text not null references public.listings(etsy_id) on delete cascade,
    run_id      text,
    captured_at timestamptz not null default now(),
    favorites   int,
    price_usd   numeric(10,2),
    reviews     int,
    rating      numeric(3,2),
    unique (etsy_id, run_id)
);
create index if not exists snapshots_etsy_idx on public.snapshots (etsy_id, captured_at desc);
create index if not exists snapshots_time_idx on public.snapshots (captured_at desc);

-- which keywords surfaced a listing, and its best rank
create table if not exists public.listing_keywords (
    etsy_id   text not null references public.listings(etsy_id) on delete cascade,
    keyword   text not null,
    best_rank int,
    last_seen timestamptz not null default now(),
    primary key (etsy_id, keyword)
);

-- ────────────────────────────────── runs ─────────────────────────────────────
create table if not exists public.runs (
    id            bigserial primary key,
    run_id        text unique not null,
    keyword       text,
    page_from     int,
    page_to       int,
    status        text,
    stopped_reason text,
    listings      int default 0,
    rows_written  int default 0,
    challenges    int default 0,
    egress_ip     text,
    gha_url       text,
    note          text,
    started_at    timestamptz,
    finished_at   timestamptz
);
create index if not exists runs_time_idx on public.runs (started_at desc);

-- ───────────────────────────────── settings ──────────────────────────────────
create table if not exists public.settings (
    key   text primary key,
    value jsonb not null
);

-- ─────────────────────────── claim / release slices ──────────────────────────
-- Picks the keyword that has gone longest without a turn (round-robin), so a
-- queue of ten keywords refreshes evenly instead of starving the last one.
create or replace function public.claim_slice(p_pages int default null)
returns table (keyword text, page_from int, page_to int, depth int, pages int)
language plpgsql
security definer
set search_path = public
as $$
declare
    k     public.keywords;
    pages int;
begin
    select * into k
      from public.keywords
     where status in ('queued', 'running')
     order by coalesce(last_run_at, 'epoch'::timestamptz) asc, priority desc, id asc
     limit 1
     for update skip locked;

    if not found then
        return;
    end if;

    -- a finished pass rolls over to page 1 of the next pass
    if k.next_page > k.depth then
        update public.keywords
           set pass_no = k.pass_no + 1, next_page = 1
         where id = k.id
        returning * into k;
    end if;

    pages := greatest(1, least(coalesce(p_pages, k.pages_per_run), k.depth - k.next_page + 1));

    update public.keywords
       set status = 'running', last_run_at = now()
     where id = k.id;

    return query select k.keyword, k.next_page, k.next_page + pages - 1, k.depth, pages;
end;
$$;

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
           status        = coalesce(p_status,
                              case when p_status is null
                                        and p_next_page > depth then 'queued'
                                        else 'running' end),
           finished_at   = now(),
           listings_seen = listings_seen + greatest(0, p_listings),
           pages_done    = pages_done + 1
     where keyword = p_keyword;
end;
$$;

-- ─────────────────────────────── batch ingest ────────────────────────────────
-- One call per ~25 listings. Previous values are copied into prev_* before the
-- new observation lands, which is what powers "+18 favourites since last look".
create or replace function public.ingest_batch(p_run_id text, p_rows jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    r          jsonb;
    v_new      int := 0;
    v_updated  int := 0;
    v_snaps    int := 0;
    v_lid      text;
begin
    for r in select * from jsonb_array_elements(p_rows)
    loop
        v_lid := nullif(r->>'listing_id', '');
        continue when v_lid is null;

        insert into public.listings as l (
            etsy_id, url, title, shop_name, shop_url, price_usd, currency,
            favorites, reviews, rating, image_url, category_path, description,
            is_bestseller, is_star_seller, free_shipping, first_keyword
        ) values (
            v_lid, r->>'url', r->>'title', r->>'shop_name', r->>'shop_url',
            nullif(r->>'price_usd', '')::numeric,
            coalesce(nullif(r->>'currency', ''), 'USD'),
            nullif(r->>'favorites', '')::int,
            nullif(r->>'listing_review_count', '')::int,
            nullif(r->>'listing_rating', '')::numeric,
            r->>'thumbnail_url', r->>'category_path', r->>'description',
            (r->>'is_bestseller')::boolean, (r->>'is_star_seller')::boolean,
            (r->>'free_shipping')::boolean, r->>'keyword'
        )
        on conflict (etsy_id) do update set
            prev_favorites = l.favorites,
            prev_price     = l.price_usd,
            favorites      = excluded.favorites,
            price_usd      = excluded.price_usd,
            reviews        = coalesce(excluded.reviews, l.reviews),
            rating         = coalesce(excluded.rating, l.rating),
            title          = coalesce(excluded.title, l.title),
            shop_name      = coalesce(excluded.shop_name, l.shop_name),
            shop_url       = coalesce(excluded.shop_url, l.shop_url),
            image_url      = coalesce(excluded.image_url, l.image_url),
            category_path  = coalesce(excluded.category_path, l.category_path),
            description    = coalesce(excluded.description, l.description),
            is_bestseller  = excluded.is_bestseller,
            is_star_seller = excluded.is_star_seller,
            free_shipping  = excluded.free_shipping,
            last_seen      = now(),
            obs_count      = l.obs_count + 1;

        if found then
            v_updated := v_updated + 1;
        end if;

        insert into public.snapshots as s (etsy_id, run_id, captured_at, favorites, price_usd, reviews, rating)
        values (
            v_lid, p_run_id,
            coalesce(nullif(r->>'captured_at', '')::timestamptz, now()),
            nullif(r->>'favorites', '')::int,
            nullif(r->>'price_usd', '')::numeric,
            nullif(r->>'listing_review_count', '')::int,
            nullif(r->>'listing_rating', '')::numeric
        )
        on conflict (etsy_id, run_id) do nothing;
        if found then v_snaps := v_snaps + 1; end if;

        insert into public.listing_keywords as lk (etsy_id, keyword, best_rank, last_seen)
        values (v_lid, coalesce(r->>'keyword', 'unknown'), nullif(r->>'position', '')::int, now())
        on conflict (etsy_id, keyword) do update set
            best_rank = least(coalesce(lk.best_rank, excluded.best_rank), coalesce(excluded.best_rank, lk.best_rank)),
            last_seen = now();
    end loop;

    return jsonb_build_object('rows', v_updated, 'snapshots', v_snaps);
end;
$$;

-- ─────────────────────────────── maintenance ─────────────────────────────────
-- Free tier is 500 MB. Keep every observation for a week, then one per day,
-- then one per week — enough to chart a listing's life without storing 12 rows
-- a day forever.
create or replace function public.prune_snapshots(p_keep_days int default 7)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare v_deleted int;
begin
    with ranked as (
        select id,
               row_number() over (
                   partition by etsy_id, date_trunc('day', captured_at)
                   order by captured_at desc
               ) as rn,
               captured_at
          from public.snapshots
    )
    delete from public.snapshots s
     using ranked
     where s.id = ranked.id
       and ranked.rn > 1
       and ranked.captured_at < now() - (p_keep_days || ' days')::interval;
    get diagnostics v_deleted = row_count;
    return v_deleted;
end;
$$;

-- ──────────────────────────── writes from the web UI ─────────────────────────
create or replace function public.check_token(p_token text)
returns boolean language plpgsql security definer set search_path = public as $$
declare v_ok boolean;
begin
    select (value->>'sha256') = encode(extensions.digest(coalesce(p_token, ''), 'sha256'), 'hex')
      into v_ok
      from public.settings where key = 'write_token';
    return coalesce(v_ok, false);
end;
$$;

create or replace function public.add_keyword(
    p_token   text,
    p_keyword text,
    p_depth   int default 20,
    p_pages_per_run int default 2
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
    insert into public.keywords as k (keyword, depth, pages_per_run, status, added_by)
    values (v_kw, greatest(1, least(100, p_depth)), greatest(1, least(5, p_pages_per_run)), 'queued', 'web')
    on conflict (keyword) do update set
        status = case when k.status = 'paused' then 'queued' else k.status end,
        depth  = greatest(k.depth, excluded.depth);
    return jsonb_build_object('ok', true, 'keyword', v_kw);
end;
$$;

create or replace function public.set_keyword_status(p_token text, p_keyword text, p_status text)
returns void language plpgsql security definer set search_path = public as $$
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    if p_status not in ('queued','paused','done','blocked') then
        raise exception 'bad status %', p_status;
    end if;
    update public.keywords
       set status = p_status,
           next_page = case when p_status = 'queued' and next_page > depth then 1 else next_page end
     where keyword = p_keyword;
end;
$$;

create or replace function public.delete_keyword(p_token text, p_keyword text)
returns void language plpgsql security definer set search_path = public as $$
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    delete from public.keywords where keyword = p_keyword;
end;
$$;

create or replace function public.set_star(p_token text, p_etsy_id text, p_on boolean)
returns void language plpgsql security definer set search_path = public as $$
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    update public.listings set starred = p_on where etsy_id = p_etsy_id;
end;
$$;

-- ──────────────────────────────── dashboard ──────────────────────────────────
create or replace view public.stats with (security_invoker = on) as
select (select count(*) from public.listings)                    as listings,
       (select count(*) from public.snapshots)                   as snapshots,
       (select count(*) from public.keywords where status in ('queued','running')) as active_keywords,
       (select count(*) from public.keywords)                    as keywords,
       (select count(*) from public.runs)                        as runs,
       (select max(last_seen) from public.listings)              as last_seen,
       (select count(*) from public.listings where starred)      as starred;

-- ─────────────────────────────────── RLS ─────────────────────────────────────
-- Anonymous (the public web UI) may read; only the service role may write.
-- The three mutation RPCs are the sole door, and they check the write token.
alter table public.listings         enable row level security;
alter table public.snapshots        enable row level security;
alter table public.keywords         enable row level security;
alter table public.listing_keywords enable row level security;
alter table public.runs             enable row level security;
alter table public.settings         enable row level security;

drop policy if exists read_listings on public.listings;
create policy read_listings on public.listings for select to anon, authenticated using (true);
drop policy if exists read_snapshots on public.snapshots;
create policy read_snapshots on public.snapshots for select to anon, authenticated using (true);
drop policy if exists read_keywords on public.keywords;
create policy read_keywords on public.keywords for select to anon, authenticated using (true);
drop policy if exists read_lk on public.listing_keywords;
create policy read_lk on public.listing_keywords for select to anon, authenticated using (true);
drop policy if exists read_runs on public.runs;
create policy read_runs on public.runs for select to anon, authenticated using (true);
-- settings stays invisible to anon: it holds the write-token hash.

grant usage on schema public to anon, authenticated;
grant select on public.listings, public.snapshots, public.keywords,
                public.listing_keywords, public.runs, public.stats to anon, authenticated;
grant execute on function public.add_keyword(text, text, int, int) to anon, authenticated;
grant execute on function public.set_keyword_status(text, text, text) to anon, authenticated;
grant execute on function public.delete_keyword(text, text) to anon, authenticated;
grant execute on function public.set_star(text, text, boolean) to anon, authenticated;

insert into public.settings (key, value)
values ('retention', '{"keep_days": 7, "max_snapshots": 250000}'::jsonb)
on conflict (key) do nothing;

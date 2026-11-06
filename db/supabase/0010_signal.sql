-- 0010 — more signal per page load
--
-- Everything here was derived from saved Etsy HTML in data/runs/*/art/html, not
-- from guesswork:
--
--   * "Listed on Nov 3, 2026"      div.wt-text-caption, stable across re-crawls
--   * bestseller badge            div#bestseller clg-icon[name=bestseller]
--   * star-seller badge           p.wt-text-title-01 == "Star Seller" (the help
--                                 tooltip says "Star Sellers have an outstanding
--                                 track record…" on EVERY page, which is why the
--                                 old whole-page regex reported 44% star sellers)
--   * product JSON-LD             image[], offers.availability, eligibleQuantity,
--                                 shippingDetails.shippingOrigin, aggregateRating
--   * search cards                "Sale Price … Original Price … (50% off)", "Ad"
--   * related searches            a[href*="search?q="] chips, ~12 per page
--
-- Two semantics worth stating out loud:
--   two different review counts live on one page and must not be conflated:
--     listing reviews  JSON-LD aggregateRating.reviewCount  (e.g. 2,253)
--     shop reviews     embedded "shop_reviews_count"        (e.g. 15,362)

alter table public.listings add column if not exists listing_date      date;
alter table public.listings add column if not exists image_count       int;
alter table public.listings add column if not exists availability      text;
alter table public.listings add column if not exists quantity          int;
alter table public.listings add column if not exists shipping_origin   text;
alter table public.listings add column if not exists shop_reviews      int;
alter table public.listings add column if not exists shop_rating       numeric;
alter table public.listings add column if not exists discount_pct      numeric;
alter table public.listings add column if not exists original_price_usd numeric;
alter table public.listings add column if not exists is_ad             boolean;

comment on column public.listings.reviews is
    'Listing-level review count (JSON-LD aggregateRating.reviewCount). Shop-level lives in shop_reviews.';
comment on column public.listings.listing_date is
    'The "Listed on <date>" line on the listing page; verified stable across re-crawls.';
comment on column public.listings.is_ad is
    'Card was marked "Ad" in search results — exclude from organic rank analysis.';

create index if not exists listings_listing_date_idx on public.listings (listing_date desc);
create index if not exists listings_shop_idx on public.listings (shop_name);

-- ─────────────────────────── keyword candidates ──────────────────────────────
-- Every search page advertises ~12 related queries. They are free (already
-- downloaded) and they are the market naming its own niches, so the crawler
-- collects them and the queue can promote whichever ones the data likes.
create table if not exists public.keyword_candidates (
    keyword          text primary key,
    source           text not null default 'related-search',
    hits             int  not null default 1,
    first_seen       timestamptz not null default now(),
    last_seen        timestamptz not null default now(),
    median_favorites numeric,
    status           text not null default 'new'
                     check (status in ('new', 'queued', 'dismissed'))
);

alter table public.keyword_candidates enable row level security;
revoke all on public.keyword_candidates from anon;

create or replace function public.note_candidates(
    p_keywords text[],
    p_source   text default 'related-search'
) returns int
language plpgsql
security definer
set search_path = public
as $$
declare v int := 0;
begin
    with ins as (
        insert into public.keyword_candidates as c (keyword, source)
        select distinct lower(trim(k)), p_source
          from unnest(p_keywords) as k
         where length(trim(k)) between 2 and 80
        on conflict (keyword) do update
           set hits = c.hits + 1, last_seen = now()
        returning 1
    )
    select count(*) into v from ins;
    return coalesce(v, 0);
end;
$$;

revoke execute on function public.note_candidates(text[], text) from public, anon, authenticated;
grant  execute on function public.note_candidates(text[], text) to service_role;

-- ─────────────────────────── market table ────────────────────────────────────
-- One row per keyword per day. This is what turns 4,000 rows into a decision:
-- demand (favorites) against competition (share of low-review sellers).
create or replace view public.keyword_daily
with (security_invoker = true) as
select r.keyword,
       date_trunc('day', s.captured_at)::date                     as day,
       count(distinct s.etsy_id)                                  as listings,
       round(percentile_cont(0.5) within group (order by s.price_usd)::numeric, 2)  as median_price,
       round(percentile_cont(0.25) within group (order by s.price_usd)::numeric, 2) as p25_price,
       round(percentile_cont(0.75) within group (order by s.price_usd)::numeric, 2) as p75_price,
       round(percentile_cont(0.5) within group (order by s.favorites)::numeric, 0)  as median_fav,
       round(percentile_cont(0.9) within group (order by s.favorites)::numeric, 0)  as p90_fav,
       round(max(s.favorites)::numeric, 0)                        as max_fav,
       round(avg((coalesce(l.reviews, 0) < 50)::int)::numeric, 3) as share_low_reviews,
       round(avg((coalesce(l.is_bestseller, false))::int)::numeric, 3) as share_bestseller,
       round(avg((l.listing_date >= current_date - 90)::int)::numeric, 3) as share_listed_90d,
       count(distinct l.shop_name)                                as shops
  from public.snapshots s
  join public.runs r     on r.run_id = s.run_id
  join public.listings l on l.etsy_id = s.etsy_id
 group by 1, 2;

grant select on public.keyword_daily to authenticated, service_role;

-- ─────────────────────────── ingest ──────────────────────────────────────────
-- Same contract as before, plus the new columns. Kept as a full replacement so
-- there is exactly one ingest path.
create or replace function public.ingest_batch(p_run_id text, p_rows jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    r         jsonb;
    v_new     int := 0;
    v_updated int := 0;
    v_snaps   int := 0;
    v_lid     text;
begin
    for r in select * from jsonb_array_elements(p_rows)
    loop
        v_lid := nullif(r->>'listing_id', '');
        continue when v_lid is null;

        insert into public.listings as l (
            etsy_id, url, title, shop_name, shop_url, price_usd, currency,
            favorites, reviews, rating, image_url, category_path, description,
            is_bestseller, is_star_seller, free_shipping, first_keyword,
            listing_date, image_count, availability, quantity, shipping_origin,
            shop_reviews, shop_rating, discount_pct, original_price_usd, is_ad
        ) values (
            v_lid, r->>'url', r->>'title', r->>'shop_name', r->>'shop_url',
            nullif(r->>'price_usd', '')::numeric,
            coalesce(nullif(r->>'currency', ''), 'USD'),
            nullif(r->>'favorites', '')::int,
            nullif(r->>'listing_review_count', '')::int,
            nullif(r->>'listing_rating', '')::numeric,
            r->>'thumbnail_url', r->>'category_path', r->>'description',
            (nullif(r->>'is_bestseller', ''))::boolean,
            (nullif(r->>'is_star_seller', ''))::boolean,
            (nullif(r->>'free_shipping', ''))::boolean,
            r->>'keyword',
            nullif(r->>'listing_date', '')::date,
            nullif(r->>'image_count', '')::int,
            nullif(r->>'availability', ''),
            nullif(r->>'quantity', '')::int,
            nullif(r->>'shipping_origin', ''),
            nullif(r->>'shop_review_count', '')::int,
            nullif(r->>'shop_rating', '')::numeric,
            nullif(r->>'discount_pct', '')::numeric,
            nullif(r->>'price_original_usd', '')::numeric,
            (nullif(r->>'is_ad', ''))::boolean
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
            is_bestseller  = coalesce(excluded.is_bestseller, l.is_bestseller),
            is_star_seller = coalesce(excluded.is_star_seller, l.is_star_seller),
            free_shipping  = coalesce(excluded.free_shipping, l.free_shipping),
            listing_date   = coalesce(excluded.listing_date, l.listing_date),
            image_count    = coalesce(excluded.image_count, l.image_count),
            availability   = coalesce(excluded.availability, l.availability),
            quantity       = coalesce(excluded.quantity, l.quantity),
            shipping_origin = coalesce(excluded.shipping_origin, l.shipping_origin),
            shop_reviews   = coalesce(excluded.shop_reviews, l.shop_reviews),
            shop_rating    = coalesce(excluded.shop_rating, l.shop_rating),
            discount_pct   = coalesce(excluded.discount_pct, l.discount_pct),
            original_price_usd = coalesce(excluded.original_price_usd, l.original_price_usd),
            is_ad          = coalesce(excluded.is_ad, l.is_ad),
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

revoke execute on function public.ingest_batch(text, jsonb) from public, anon, authenticated;
grant  execute on function public.ingest_batch(text, jsonb) to service_role;

-- 0012 — what the crawler already knows about *time*, turned into answers.
--
-- Everything here is derived from columns that are already stored, so it costs
-- zero extra page loads:
--
--   listings.listing_date   Etsy's own "Listed on …" caption on the detail page
--   listings.first_seen     when this crawler first saw the listing
--   snapshots.favorites     one reading per crawl, so a real delta exists
--   listing_keywords.best_rank  the best search position the listing was seen at
--   listings.is_ad          sponsored slots, which sit in the same result list
--
-- The point of the views is one question: **of the listings that are new, which
-- ones are already winning, and what are they selling?** Favourites are the
-- demand proxy — Etsy does not publish sales — so every view says so.

-- ---------------------------------------------------------------------------
-- 1. Per-listing time maths: age, favourites per day, real observed velocity.
--    fav_per_day is deliberately floored at one day: a listing created eight
--    hours ago must not divide by 0.1 and look like a rocket.
-- ---------------------------------------------------------------------------
create or replace view public.listing_stats as
with snap as (
  select etsy_id,
         min(captured_at) as first_at,
         max(captured_at) as last_at,
         count(*)        as readings,
         min(favorites)  as fav_min,
         max(favorites)  as fav_max
  from public.snapshots
  where favorites is not null
  group by etsy_id
)
select l.etsy_id,
       l.title,
       l.shop_name,
       l.price_usd,
       l.favorites,
       l.reviews,
       l.rating,
       l.is_ad,
       l.listing_date,
       l.first_seen,
       l.last_seen,
       l.image_count,
       coalesce(l.shop_reviews, l.reviews) as seller_reviews,
       -- Age comes from Etsy's own date or it does not exist. Substituting
       -- first_seen would turn every legacy row into a brand-new listing and
       -- make favourites/day meaningless, so unknown stays unknown.
       l.listing_date as age_anchor,
       (l.listing_date is null) as age_is_estimated,
       case when l.listing_date is not null
            then greatest(0, current_date - l.listing_date) end as age_days,
       case when l.listing_date is not null
            then round(l.favorites::numeric / greatest(current_date - l.listing_date, 1), 2) end
         as fav_per_day,
       case when l.listing_date is null then 'unknown' else 'etsy_listed_on' end as age_basis,
       s.readings,
       s.first_at,
       s.last_at,
       s.fav_min,
       s.fav_max,
       round((s.fav_max - s.fav_min)::numeric
             / greatest(extract(epoch from (s.last_at - s.first_at)) / 86400.0, 0.25), 2) as fav_per_day_observed,
       greatest(0, current_date - l.first_seen::date) as tracked_days
from public.listings l
left join snap s on s.etsy_id = l.etsy_id;

comment on view public.listing_stats is
  'Per-listing age and demand velocity. fav_per_day = favourites / days since Etsy listed it (floored at 1). fav_per_day_observed = the slope between this crawler''s own readings, which needs at least two crawls.';

-- ---------------------------------------------------------------------------
-- 2. age_cohorts — does Etsy actually let new listings rank?
--    For each keyword, bucket the listings by age and report how many of each
--    bucket reach the top 20 and how much demand they carry. A keyword where the
--    0–7 day bucket is absent from the top 20 is a closed market.
-- ---------------------------------------------------------------------------
create or replace view public.age_cohorts as
with base as (
  select lk.keyword,
         lk.best_rank,
         st.age_days,
         st.favorites,
         st.fav_per_day,
         st.price_usd,
         case
           when st.age_days <= 7  then '0-7d'
           when st.age_days <= 30 then '8-30d'
           when st.age_days <= 90 then '31-90d'
           when st.age_days <= 365 then '91-365d'
           else '365d+'
         end as cohort
  from public.listing_keywords lk
  join public.listing_stats st using (etsy_id)
  where st.age_days is not null      -- no listing date, no cohort: never impute
)
select keyword,
       cohort,
       count(*)                                                   as listings,
       count(*) filter (where best_rank <= 20)                    as in_top20,
       round(100.0 * count(*) filter (where best_rank <= 20) / nullif(count(*), 0), 1) as pct_in_top20,
       round(percentile_cont(0.5) within group (order by favorites)::numeric, 0)  as median_favorites,
       round(percentile_cont(0.5) within group (order by fav_per_day)::numeric, 2) as median_fav_per_day,
       round(percentile_cont(0.9) within group (order by fav_per_day)::numeric, 2) as p90_fav_per_day,
       round(percentile_cont(0.5) within group (order by price_usd)::numeric, 2)   as median_price
from base
group by keyword, cohort;

comment on view public.age_cohorts is
  'Listings bucketed by age, with the share that reach the top 20 and demand per day. Shows whether newcomers can rank for a keyword.';

-- ---------------------------------------------------------------------------
-- 3. new_winners — the point of the whole exercise.
--    Recently listed products already pulling attention, ranked by favourites
--    per day rather than raw favourites (raw favourites just finds old winners).
-- ---------------------------------------------------------------------------
create or replace view public.new_winners as
select lk.keyword,
       st.etsy_id,
       st.title,
       st.shop_name,
       st.price_usd,
       st.favorites,
       st.age_days,
       st.age_basis,
       st.fav_per_day,
       st.fav_per_day_observed,
       lk.best_rank,
       st.is_ad,
       st.seller_reviews,
       st.listing_date,
       st.image_count
from public.listing_keywords lk
join public.listing_stats st using (etsy_id)
where st.age_days <= 90                 -- implies a real Etsy listing date
  and coalesce(st.favorites, 0) >= 10
  and lk.best_rank is not null;

comment on view public.new_winners is
  'Listings under 90 days old with at least 10 favourites, ranked by favourites per day. The shortlist for "what is working right now".';

-- ---------------------------------------------------------------------------
-- 4. shop_concentration — how owned is the keyword? HHI over the sampled shops.
--    HHI near 1 = one shop owns the page; near 0 = a long tail.
-- ---------------------------------------------------------------------------
create or replace view public.shop_concentration as
with per_shop as (
  select lk.keyword, l.shop_name, count(*)::numeric as n
  from public.listing_keywords lk
  join public.listings l using (etsy_id)
  where l.shop_name is not null
  group by 1, 2
), tot as (
  select keyword, sum(n) as total, count(*) as shops from per_shop group by 1
)
select p.keyword,
       t.shops,
       t.total::int as listings,
       round(sum((p.n / t.total) ^ 2)::numeric, 3) as hhi,
       round(max(p.n / t.total)::numeric, 3)      as top_shop_share
from per_shop p
join tot t using (keyword)
group by p.keyword, t.shops, t.total;

comment on view public.shop_concentration is
  'Herfindahl index of shops per keyword, over the sampled listings. High = concentrated = hard to break in.';

-- ---------------------------------------------------------------------------
-- 5. phrase_stats — the creative surface: which words and two-word phrases
--    appear in the listings that are winning? Product words are removed so the
--    result is about design and audience, not "shirt".
-- ---------------------------------------------------------------------------
create or replace view public.phrase_stats as
with stop(w) as (
  values ('the'),('and'),('for'),('with'),('you'),('your'),('our'),('his'),('her'),('their'),
         ('from'),('this'),('that'),('are'),('was'),('not'),('but'),('all'),('any'),('can'),
         ('day'),('new'),('one'),('two'),('set'),('size'),('sizes'),('color'),('colors'),
         ('shirt'),('shirts'),('tshirt'),('tshirts'),('tee'),('tees'),('t-shirt'),('t'),
         ('top'),('tops'),('apparel'),('clothing'),('unisex'),('women'),('mens'),('womens'),
         ('men'),('kids'),('adult'),('cotton'),('soft'),('gift'),('gifts'),('perfect'),
         ('made'),('handmade'),('custom'),('personalized'),('personalised'),
         ('more'),('free'),('shipping'),('sale'),('new'),('hot'),('best'),('item')
),
tok as (
  select lk.keyword,
         l.etsy_id,
         l.favorites,
         l.price_usd,
         l.listing_date,
         x.w,
         x.ord
  from public.listing_keywords lk
  join public.listings l using (etsy_id)
  cross join lateral regexp_split_to_table(lower(coalesce(l.title, '')), '[^a-z0-9]+')
       with ordinality as x(w, ord)
),
clean as (
  select keyword, etsy_id, favorites, price_usd, listing_date, w, ord
  from tok
  where length(w) between 3 and 24
    and w !~ '^[0-9]+$'
    and w not in (select w from stop)
),
uni as (
  select keyword, w as phrase, 1 as words, count(distinct etsy_id) as listings,
         percentile_cont(0.5) within group (order by favorites) as median_fav,
         percentile_cont(0.9) within group (order by favorites) as p90_fav,
         percentile_cont(0.5) within group (order by price_usd) as median_price,
         count(*) filter (where listing_date >= current_date - 30) as new_count
  from clean
  where favorites is not null
  group by 1, 2
),
bi as (
  select keyword, w || ' ' || nxt as phrase, 2 as words, count(distinct etsy_id) as listings,
         percentile_cont(0.5) within group (order by favorites) as median_fav,
         percentile_cont(0.9) within group (order by favorites) as p90_fav,
         percentile_cont(0.5) within group (order by price_usd) as median_price,
         count(*) filter (where listing_date >= current_date - 30) as new_count
  from (
    select keyword, etsy_id, favorites, price_usd, listing_date, w,
           lead(w) over (partition by keyword, etsy_id order by ord) as nxt
    from clean
  ) q
  where favorites is not null and nxt is not null
  group by 1, 2
)
select keyword, phrase, words, listings,
       round(median_fav::numeric, 0)   as median_favorites,
       round(p90_fav::numeric, 0)      as p90_favorites,
       round(median_price::numeric, 2) as median_price,
       round(100.0 * new_count / nullif(listings, 0), 0) as pct_listed_last_30d
from (
  select * from uni
  union all
  select * from bi
) u
where listings >= 3;

comment on view public.phrase_stats is
  'Title words and bigrams with their demand, after removing product and filler words. Answers "what are the winning designs about", not "what is the product".';

-- ---------------------------------------------------------------------------
-- 6. keyword_entry — one number per keyword, from the four things that decide
--    whether a new shop can win: price, weak sellers, newcomer access, and how
--    concentrated the page is. Weights are explicit so they can be argued with.
-- ---------------------------------------------------------------------------
create or replace view public.keyword_entry as
with k as (
  select kd.keyword,
         kd.listings,
         kd.median_price,
         kd.p90_fav,
         kd.share_low_reviews
  from public.keyword_daily kd
  join (select keyword, max(day) as day from public.keyword_daily group by 1) latest
    on latest.keyword = kd.keyword and latest.day = kd.day
), c as (
  select * from public.shop_concentration
), nc as (
  select keyword,
         round(100.0 * count(*) filter (where age_days <= 30 and best_rank <= 30) / nullif(count(*), 0), 1) as pct_new_in_top30
  from public.listing_keywords lk join public.listing_stats st using (etsy_id)
  group by keyword
)
select k.keyword,
       k.listings,
       k.median_price,
       k.p90_fav,
       round(k.share_low_reviews, 2)  as weak_seller_share,
       coalesce(nc.pct_new_in_top30, 0) as pct_new_in_top30,
       coalesce(c.hhi, 0)             as shop_hhi,
       round((
          0.30 * least(k.share_low_reviews, 1)
        + 0.25 * least(coalesce(nc.pct_new_in_top30, 0) / 50.0, 1)
        + 0.20 * (1 - least(coalesce(c.hhi, 0), 1))
        + 0.15 * least(k.median_price / 40.0, 1)
        + 0.10 * least(k.p90_fav / 2000.0, 1)
       ) * 100, 1) as entry_score
from k
left join c using (keyword)
left join nc using (keyword);

comment on view public.keyword_entry is
  'Composite 0-100 entry score per keyword: 30% weak sellers, 25% newcomers reaching the top 30, 20% shop fragmentation, 15% price, 10% demand ceiling. Weights are a judgement call, stated so they can be changed.';

-- ---------------------------------------------------------------------------
-- 7. launch_curve — how many listings in the sample were launched each week.
--    A rising curve is a niche sellers are piling into; a falling one is stale.
-- ---------------------------------------------------------------------------
create or replace view public.launch_curve as
select lk.keyword,
       date_trunc('week', st.age_anchor)::date as week,
       count(*) as listings,
       round(percentile_cont(0.5) within group (order by st.favorites)::numeric, 0) as median_favorites,
       round(percentile_cont(0.5) within group (order by st.fav_per_day)::numeric, 2) as median_fav_per_day
from public.listing_keywords lk
join public.listing_stats st using (etsy_id)
where st.age_anchor is not null
group by 1, 2;

-- Indexes the views lean on.
create index if not exists listings_listing_date_idx on public.listings (listing_date);
create index if not exists listing_keywords_rank_idx  on public.listing_keywords (keyword, best_rank);

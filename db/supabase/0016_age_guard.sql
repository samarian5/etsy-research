-- 0016 — an age that cannot be true must not be used.
--
-- The backfill works: dated coverage went 3.3% → 5.7% in one tick and is still
-- climbing. But the first real numbers exposed a flaw in the input, not the maths:
--
--   1425039325  listed 2026-11-08, first seen 2026-11-04   ← impossible
--    771800607  listed 2026-11-04, id from ~2019, 6.2k favourites
--
-- The first is a misparse (a date lifted from somewhere else on the page). The
-- second is a *re-list*: Etsy keeps the listing id and the favourites and resets
-- the displayed date, so the age is real but the favourites did not accumulate
-- during it. Both make `fav_per_day` nonsense — 15,098 favourites on a "0 day
-- old" listing.
--
-- Two invariants are enough, and both come from data already stored:
--   * a listing cannot be published after the moment we first saw it;
--   * a publish date before Etsy's own marketplace era is a parse error.
--
-- Rows that fail either test keep their raw date and are marked, not deleted —
-- so the damage is inspectable — but they are excluded from every age-based
-- ranking. Re-lists are additionally recognisable by `relisted` once the parser
-- flags them (crawler change in the same commit).

alter table public.listings add column if not exists relisted boolean;

-- The column list grows here (age_plausible, relisted), and CREATE OR REPLACE
-- cannot rename or reorder view columns — so drop and rebuild the family.
drop view if exists public.new_winners, public.age_cohorts, public.launch_curve,
                    public.listing_stats cascade;
create view public.listing_stats as
with snap as (
    select etsy_id,
           min(captured_at) as first_at,
           max(captured_at) as last_at,
           count(*)         as readings,
           min(favorites)   as fav_min,
           max(favorites)   as fav_max
      from public.snapshots
     where favorites is not null
     group by etsy_id
), base as (
    select l.*,
           coalesce(s.readings, 0) as readings,
           s.first_at, s.last_at, s.fav_min, s.fav_max,
           (l.listing_date is not null
            and l.listing_date >= date '2015-01-01'
            and l.listing_date <= l.first_seen::date)        as age_plausible
      from public.listings l
      left join snap s on s.etsy_id = l.etsy_id
)
select b.etsy_id,
       b.title,
       b.shop_name,
       b.price_usd,
       b.favorites,
       b.reviews,
       b.rating,
       b.is_ad,
       b.listing_date,
       b.first_seen,
       b.last_seen,
       b.image_count,
       coalesce(b.shop_reviews, b.reviews) as seller_reviews,
       b.listing_date as age_anchor,
       not b.age_plausible as age_is_estimated,
       case when b.age_plausible then greatest(0, current_date - b.listing_date) end as age_days,
       case when b.age_plausible
            then round(b.favorites::numeric
                       / greatest(current_date - b.listing_date, 1)::numeric, 2) end as fav_per_day,
       case when b.age_plausible then 'etsy_listed_on'
            when b.listing_date is null then 'unknown'
            else 'rejected: impossible date' end                       as age_basis,
       b.age_plausible,
       coalesce(b.relisted, false) as relisted,
       b.readings, b.first_at, b.last_at, b.fav_min, b.fav_max,
       -- A listing "published today" holding thousands of favourites inherited
       -- them from before its re-list: Etsy resets the date, not the favourites.
       -- Such a rate is arithmetic, not demand, until repeated readings prove the
       -- listing is actually accumulating. Keep the row, refuse to rank it.
       not (coalesce(b.favorites, 0) >= 1000
            and coalesce(current_date - b.listing_date, 99) <= 2
            and coalesce(b.readings, 0) <= 2)                     as velocity_trusted,
       round((b.fav_max - b.fav_min)::numeric
             / greatest(extract(epoch from b.last_at - b.first_at) / 86400.0, 0.25), 2) as fav_per_day_observed,
       greatest(0, current_date - b.first_seen::date) as tracked_days
  from base b;

create or replace view public.new_winners as
select lk.keyword, st.etsy_id, st.title, st.shop_name, st.price_usd, st.favorites,
       st.age_days, st.age_basis, st.fav_per_day, st.fav_per_day_observed,
       lk.best_rank, st.is_ad, st.seller_reviews, st.listing_date, st.image_count
  from public.listing_keywords lk
  join public.listing_stats st using (etsy_id)
 where st.age_plausible
   and not st.relisted
   and st.velocity_trusted
   and st.age_days <= 90
   and coalesce(st.favorites, 0) >= 10
   and lk.best_rank is not null;

create or replace view public.age_cohorts as
with base as (
    select lk.keyword, lk.best_rank, st.age_days, st.favorites, st.fav_per_day, st.price_usd,
           case when st.age_days <= 7   then '0-7d'
                when st.age_days <= 30  then '8-30d'
                when st.age_days <= 90  then '31-90d'
                when st.age_days <= 365 then '91-365d'
                else '365d+' end as cohort
      from public.listing_keywords lk
      join public.listing_stats st using (etsy_id)
     where st.age_plausible
       and not st.relisted
)
select keyword, cohort, count(*) as listings,
       count(*) filter (where best_rank <= 20) as in_top20,
       round(100.0 * count(*) filter (where best_rank <= 20)::numeric
             / nullif(count(*), 0)::numeric, 1) as pct_in_top20,
       round(percentile_cont(0.5) within group (order by favorites::double precision)::numeric, 0) as median_favorites,
       round(percentile_cont(0.5) within group (order by fav_per_day::double precision)::numeric, 2) as median_fav_per_day,
       round(percentile_cont(0.9) within group (order by fav_per_day::double precision)::numeric, 2) as p90_fav_per_day,
       round(percentile_cont(0.5) within group (order by price_usd::double precision)::numeric, 2) as median_price
  from base
 group by keyword, cohort;

create or replace view public.launch_curve as
select lk.keyword,
       date_trunc('week', st.age_anchor::timestamptz)::date as week,
       count(*) as listings,
       round(percentile_cont(0.5) within group (order by st.favorites::double precision)::numeric, 0) as median_favorites,
       round(percentile_cont(0.5) within group (order by st.fav_per_day::double precision)::numeric, 2) as median_fav_per_day
  from public.listing_keywords lk
  join public.listing_stats st using (etsy_id)
 where st.age_plausible
   and not st.relisted
 group by lk.keyword, date_trunc('week', st.age_anchor::timestamptz)::date;

alter view public.listing_stats set (security_invoker = true);
alter view public.new_winners   set (security_invoker = true);
alter view public.age_cohorts   set (security_invoker = true);
alter view public.launch_curve  set (security_invoker = true);

revoke all on public.listing_stats from anon;
revoke all on public.new_winners   from anon;
revoke all on public.age_cohorts   from anon;
revoke all on public.launch_curve  from anon;

grant select on public.listing_stats to authenticated;
grant select on public.new_winners   to authenticated;
grant select on public.age_cohorts   to authenticated;
grant select on public.launch_curve  to authenticated;

notify pgrst, 'reload schema';

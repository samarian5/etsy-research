-- 0018 — make the design views fast enough to serve from the browser.
--
-- `listing_niche` matched 2,600 titles against 29 regexes. That is fine as a
-- one-off query and fatal as an HTTP endpoint: PostgREST applies LIMIT *after*
-- the view, so `?limit=6` still evaluated every row and the request died on the
-- statement timeout (57014) — the panel rendered empty.
--
-- The niche assigned to a listing only changes when its title changes, so the
-- mapping is materialised and refreshed once per crawl instead of recomputed on
-- every page load. Everything downstream stays a plain view over the matview.

drop view if exists public.niche_examples;
drop view if exists public.design_briefs;
drop view if exists public.niche_stats;
drop view if exists public.listing_niche;
drop materialized view if exists public.listing_niche_mv;

create materialized view public.listing_niche_mv as
select s.keyword, s.etsy_id, n.niche, s.best_rank, s.favorites, s.price_usd,
       s.shop_name, s.listing_date, s.image_url, s.title
  from public.shirt_listings s
  join lateral (
        select dn.niche
          from public.design_niches dn
         where s.title ~* dn.pattern
         order by dn.priority
         limit 1
  ) n on true;

-- One row per (keyword, listing): required for REFRESH ... CONCURRENTLY, which
-- keeps the read path available while a crawl refreshes the mapping.
create unique index listing_niche_mv_key on public.listing_niche_mv (keyword, etsy_id);
create index listing_niche_mv_niche on public.listing_niche_mv (keyword, niche, favorites desc nulls last);

create view public.listing_niche as select * from public.listing_niche_mv;

create view public.niche_stats as
select keyword, niche,
       count(*) as listings,
       count(distinct shop_name) as shops,
       round(percentile_cont(0.5) within group (order by price_usd::double precision)::numeric, 2) as median_price,
       round(percentile_cont(0.5) within group (order by favorites::double precision)::numeric, 0) as median_fav,
       round(percentile_cont(0.9) within group (order by favorites::double precision)::numeric, 0) as p90_fav,
       round(percentile_cont(0.5) within group (order by favorites::double precision
             / greatest(1, current_date - listing_date))::numeric, 2) as median_fav_per_day,
       round(100.0 * count(*) filter (where best_rank <= 30) / count(*), 1) as pct_in_top30,
       round(100.0 * count(*) filter (where listing_date >= current_date - 90)
             / nullif(count(*) filter (where listing_date is not null), 0), 1) as pct_new,
       count(*) filter (where favorites is null) as unknown_fav,
       count(*) filter (where listing_date is not null) as dated
  from public.listing_niche_mv
 group by keyword, niche;

create view public.design_briefs as
with ranked as (
    select n.*, d.label, d.note,
           percent_rank() over (partition by keyword order by p90_fav nulls first)      as demand_pct,
           percent_rank() over (partition by keyword order by listings)                 as saturation_pct,
           percent_rank() over (partition by keyword order by pct_new nulls first)      as fresh_pct,
           percent_rank() over (partition by keyword order by median_price nulls first) as price_pct
      from public.niche_stats n
      join public.design_niches d using (niche)
     where n.listings >= 8
)
select keyword, niche, label, note, listings, shops, median_price, median_fav, p90_fav,
       median_fav_per_day, pct_in_top30, pct_new, unknown_fav, dated,
       case when dated >= 0.3 * listings then fresh_pct else 0.5 end as fresh_used,
       round((100 * (0.55 * demand_pct
                     + 0.25 * (case when dated >= 0.3 * listings then fresh_pct else 0.5 end)
                     + 0.20 * (1 - saturation_pct))
              * least(1.0, listings / 30.0))::numeric, 0) as opportunity,
       round((100 * demand_pct)::numeric, 0) as demand_score,
       round((100 * (1 - saturation_pct))::numeric, 0) as openness_score
  from ranked;

create view public.niche_examples as
select s.keyword, s.niche,
       e.etsy_id, e.title, e.image_url, e.price_usd, e.favorites, e.shop_name, e.best_rank
  from (select distinct keyword, niche from public.listing_niche_mv) s
  join lateral (
        select m.etsy_id, m.title, m.image_url, m.price_usd, m.favorites, m.shop_name, m.best_rank
          from public.listing_niche_mv m
         where m.keyword = s.keyword and m.niche = s.niche
           and m.favorites is not null and m.image_url is not null
         order by m.favorites desc
         limit 6
  ) e on true;

-- One call per crawl. CONCURRENTLY needs the unique index above and leaves
-- readers on the old snapshot until it finishes.
create or replace function public.refresh_design_views()
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    n bigint;
begin
    refresh materialized view concurrently public.listing_niche_mv;
    select count(*) into n from public.listing_niche_mv;
    return jsonb_build_object('listings_in_niches', n, 'at', now());
end;
$function$;

alter view public.listing_niche    set (security_invoker = true);
alter view public.niche_stats      set (security_invoker = true);
alter view public.design_briefs    set (security_invoker = true);
alter view public.niche_examples   set (security_invoker = true);

revoke all on public.listing_niche_mv from anon;
revoke all on public.listing_niche, public.niche_stats, public.design_briefs,
              public.niche_examples from anon;

grant select on public.listing_niche_mv to authenticated;
grant select on public.listing_niche, public.niche_stats, public.design_briefs,
                public.niche_examples to authenticated;

notify pgrst, 'reload schema';

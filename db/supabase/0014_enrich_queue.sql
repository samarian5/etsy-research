-- 0014 — a backfill queue for publish dates.
--
-- Why this exists: `listings.listing_date` only arrives with a *detail* page
-- load ("Listed on Nov 3, 2026"). Cards on a search page carry no date at all.
-- So the two big keywords, which were first collected card-only, sit at 0%
-- dated — and every age/freshness statistic in 0012 depends on that column.
--
-- Rather than re-crawl blindly, spend detail loads where they buy the most:
-- the listings with the most favourites first, because those are the winners
-- the freshness question is actually about.
--
-- `detail_at` is set when the page is claimed, not when it is written, so a
-- runner that dies mid-slice still releases its claims after the cooldown
-- instead of handing the same URLs back on the next tick.

alter table public.listings add column if not exists detail_at timestamptz;

create index if not exists listings_missing_date_idx
    on public.listings (favorites desc nulls last)
 where listing_date is null;

comment on column public.listings.detail_at is
    'last time a detail page was claimed for this listing; NULL = never';

create or replace function public.claim_enrich(p_limit integer default 8)
returns table(etsy_id text, url text, favorites integer)
language sql
security definer
set search_path to 'public'
as $function$
    with picked as (
        select l.etsy_id
          from public.listings l
         where l.listing_date is null
           and (l.detail_at is null or l.detail_at < now() - interval '12 hours')
         order by l.favorites desc nulls last, l.etsy_id desc
         limit greatest(1, p_limit)
           for update skip locked
    ), marked as (
        update public.listings l
           set detail_at = now()
          from picked p
         where l.etsy_id = p.etsy_id
        returning l.etsy_id, l.url, coalesce(l.favorites, 0)::integer as favorites
    )
    select etsy_id, url, favorites from marked;
$function$;

create or replace function public.enrich_health()
returns jsonb
language sql
security definer
set search_path to 'public'
as $function$
    select jsonb_build_object(
        'listings', count(*),
        'dated', count(listing_date),
        'dated_pct', round(100.0 * count(listing_date) / greatest(count(*), 1), 1),
        'never_tried', count(*) filter (where detail_at is null),
        'cooling', count(*) filter (where listing_date is null
                                     and detail_at > now() - interval '12 hours')
    )
    from public.listings;
$function$;

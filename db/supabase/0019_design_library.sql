-- 0019_design_library.sql — the "reverse engineer" half of Adrian Vonarx's 3R method.
--
-- The crawler already stores what Etsy says about a listing (price, favourites,
-- shop, badges). What the design method needs is what the *artwork* says:
--
--   * the phrase printed on the shirt (OCR of the flat design image),
--   * how the design is composed (text-only, text + graphic, graphic-only),
--   * the background treatment (solid / gradient / striped / photo) — the
--     "striped background cut out in the middle" pattern from the video,
--   * the dominant colours, so two designs can be compared without opening them,
--   * the gallery, so the design image can be shown rather than the mockup,
--   * real urgency counters: "N people have this in their carts" and
--     "N people bought this in the last 24 hours".
--
-- Nothing here guesses: every column is filled by a deterministic pass over pixels
-- and text (scripts/design_dna.py) and every unreadable field stays NULL.

alter table public.listings
  add column if not exists gallery          jsonb,      -- up to 10 CDN urls, full-size
  add column if not exists design_image_url text,       -- the image the DNA pass read
  add column if not exists carts            int,        -- "N people have this in their carts"
  add column if not exists bought_24h       int,        -- "N people bought this in the last 24 hours"
  add column if not exists design_text      text,       -- OCR, lines joined with " / "
  add column if not exists design_text_conf real,       -- mean OCR confidence, 0..1
  add column if not exists design_layout    text,       -- text_only | text_graphic | graphic_only | unreadable
  add column if not exists design_bg        text,       -- solid | gradient | striped | photo
  add column if not exists design_colours   text[],     -- top 3 hex colours, most common first
  add column if not exists design_ink       real,       -- share of non-background pixels
  add column if not exists phrase_norm      text,       -- OCR text, lowercased, punctuation stripped
  add column if not exists design_dna_at    timestamptz;

comment on column public.listings.gallery is
  'Etsy CDN urls from the page''s JSON-LD image array; il_fullxfull, resizable to il_570xN';
comment on column public.listings.design_bg is
  'Background treatment of the artwork, measured from pixels — the pattern half of a design';
comment on column public.listings.design_dna_at is
  'When the DNA pass claimed this row; NULL means still queued, so a dead runner releases it';

-- Claim queue: most-demanded unread designs first, so the corpus fills with the
-- designs that actually matter before the long tail.
create index if not exists listings_dna_missing_idx
  on public.listings ((coalesce(favorites, 0) + 40 * coalesce(bought_24h, 0) + 15 * coalesce(carts, 0)) desc)
  where design_dna_at is null;

create index if not exists listings_phrase_idx
  on public.listings (phrase_norm) where phrase_norm is not null;

create index if not exists listings_design_bg_idx
  on public.listings (design_bg) where design_bg is not null;

-- ---------------------------------------------------------------- claim queue ---
create or replace function public.claim_dna(p_limit int default 20, p_cool_minutes int default 30)
returns table (etsy_id text, title text, gallery jsonb, favorites int, price_usd numeric)
language sql
security definer
set search_path = public
as $$
  with pick as (
    select l.etsy_id
      from public.listings l
     where l.design_dna_at is null
        or l.design_dna_at < now() - make_interval(mins => p_cool_minutes)  -- released claim
     order by coalesce(l.favorites, 0) + 40 * coalesce(l.bought_24h, 0)
              + 15 * coalesce(l.carts, 0) desc
     limit greatest(1, least(p_limit, 200))
       for update skip locked
  ), claimed as (
    update public.listings l
       set design_dna_at = now()
      from pick
     where l.etsy_id = pick.etsy_id
    returning l.etsy_id, l.title, l.gallery, l.favorites, l.price_usd
  )
  select * from claimed;
$$;

create or replace function public.dna_health()
returns json language sql security definer set search_path = public as $$
  select json_build_object(
    'total',        count(*),
    'with_gallery', count(*) filter (where gallery is not null),
    'read',         count(*) filter (where design_layout is not null),
    'text_found',   count(*) filter (where design_text is not null),
    'queued',       count(*) filter (where design_dna_at is null)
  ) from public.listings;
$$;

-- ------------------------------------------------------------- design library ---
-- One row per readable design: the row the board renders.
drop view if exists public.design_patterns, public.pattern_examples,
                    public.design_library cascade;

create view public.design_library
with (security_invoker = true) as
select l.etsy_id, l.title, l.shop_name, l.price_usd, l.favorites, l.carts, l.bought_24h,
       l.listing_date, l.reviews, l.is_bestseller, l.is_star_seller,
       coalesce(l.design_image_url, l.image_url)              as image_url,
       l.design_text, l.phrase_norm, l.design_layout, l.design_bg,
       l.design_colours, l.design_ink,
       -- Demand proxy, stated in the header: Etsy publishes no sales. Cart and
       -- 24-hour purchase counts are the nearest public evidence of velocity.
       (coalesce(l.favorites, 0) + 40 * coalesce(l.bought_24h, 0)
        + 15 * coalesce(l.carts, 0))::int                     as demand,
       case when l.listing_date is not null
            then (current_date - l.listing_date) end          as age_days,
       n.niche, n.keyword
  from public.listings l
  -- listing_niche_mv is per (keyword, listing); take the strongest niche so a
  -- shirt found under three keywords is still one design in the library.
  left join lateral (
    select m.niche, m.keyword
      from public.listing_niche_mv m
     where m.etsy_id = l.etsy_id
     order by m.best_rank asc nulls last
     limit 1
  ) n on true
 where l.design_layout is not null          -- read, not merely claimed
   and l.design_layout <> 'unreadable';

-- A pattern is a phrase that appears on more than one shirt. That is the closest
-- thing Etsy publishes to "this idea has been validated": several sellers,
-- independently, shipping the same words.
create view public.design_patterns
with (security_invoker = true) as
select phrase_norm,
       count(*)                                              as listings,
       count(distinct shop_name)                             as shops,
       sum(demand)::bigint                                   as demand_sum,
       percentile_cont(0.5) within group (order by demand)::bigint as demand_median,
       percentile_cont(0.5) within group (order by price_usd)::numeric(8,2) as price_median,
       sum(favorites)::bigint                                as favorites_sum,
       max(bought_24h)                                       as best_bought_24h,
       array_remove(array_agg(distinct niche), null)         as niches,
       array_remove(array_agg(distinct design_bg), null)     as backgrounds,
       min(image_url)                                        as image_url,
       max(etsy_id)                                          as any_etsy_id,
       max(demand)                                           as top_demand
  from public.design_library
 where length(phrase_norm) between 4 and 120
 group by phrase_norm
having count(*) > 1;

-- The winners inside each pattern, for the visual check.
create view public.pattern_examples
with (security_invoker = true) as
select * from (
  select d.*, row_number() over (partition by d.phrase_norm order by d.demand desc) as rn
    from public.design_library d
   where d.phrase_norm in (select phrase_norm from public.design_patterns)
) t
where rn <= 4;

grant select on public.design_library, public.design_patterns, public.pattern_examples to authenticated;
revoke all on public.design_library, public.design_patterns, public.pattern_examples from anon;

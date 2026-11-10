-- 0021_case_briefs.sql — the design-brief method, retargeted at phone cases.
--
-- The t-shirt layer answers "which buyer-identity niche is open" with regexes over
-- shirt titles. Phone-case titles are a different language: they are search-stuffed
-- ("for iPhone 16 Pro Max, Cute Cat, MagSafe, TPU") and the buyer identity lives in
-- the *style* rather than the wearer. So cases get their own dictionary, their own
-- listings view and their own scored briefs, and none of the t-shirt numbers move.
--
-- Same scoring as 0017 so the two panels are comparable:
--   opportunity = 0.55·demand + 0.25·freshness + 0.20·openness, × min(1, listings/30)
-- freshness only counts when ≥30 % of the niche has a publish date.

create table if not exists public.case_niches (
    niche    text primary key,
    label    text not null,
    pattern  text not null,
    priority int  not null default 50,
    note     text
);

insert into public.case_niches (niche, label, pattern, priority, note) values
 ('anime',        'Anime / manga',        '\y(anime|manga|otaku|ghibli|naruto|jujutsu|demon slayer|one piece|dragon ball|attack on titan|sailor moon)\y', 5,  'Licensed characters are off-limits — read the art style, not the IP'),
 ('cat',          'Cat',                  '\y(cat|cats|kitty|kitten|meow|kawaii cat)\y', 6, null),
 ('dog',          'Dog',                  '\y(dog|dogs|puppy|pup|doggo|paw|paws)\y', 6, null),
 ('personalised', 'Custom / photo',       '\y(custom|personaliz|personaliz|monogram|initials|your name|your photo|picture|portrait|add (a )?name)\y', 7, 'Highest-intent buyer: pays more, tolerates longer delivery'),
 ('floral',       'Floral / botanical',   '\y(floral|flower|flowers|botanical|rose|roses|daisy|sunflower|tulip|cherry blossom)\y', 10, null),
 ('marble',       'Marble / abstract',    '\y(marble|granite|agate|geode|abstract|swirl)\y', 10, null),
 ('animal_print', 'Animal print',         '\y(leopard|cheetah|zebra|tiger print|cow ?print|snake ?skin|animal print|tortoise ?shell)\y', 10, null),
 ('retro',        'Retro / Y2K',          '\y(retro|vintage|y2k|90s|80s|70s|groovy|disco)\y', 10, null),
 ('western',      'Western / cowgirl',    '\y(western|cowgirl|cowboy|rodeo|southwestern|aztec|turquoise)\y', 10, null),
 ('kawaii',       'Cute / kawaii',        '\y(kawaii|kawai|sanrio|hello kitty|soft girl|pastel)\y', 10, null),
 ('coquette',     'Coquette / bows',      '\y(coquette|bow|bows|balletcore|ribbon|lace|pearl)\y', 10, null),
 ('gaming',       'Gaming',               '\y(gaming|gamer|controller|playstation|xbox|nintendo|switch)\y', 12, null),
 ('music',        'Music',                '\y(music|band|guitar|vinyl|concert|rap|hip hop|lyrics)\y', 12, null),
 ('sports',       'Sports / team',        '\y(sports?|football|baseball|basketball|soccer|hockey|team|jersey)\y', 12, null),
 ('space',        'Celestial',            '\y(space|celestial|zodiac|astrology|moon phase|starry|constellation|sun and moon)\y', 14, null),
 ('checker',      'Checkered / plaid',    '\y(checkered|checkerboard|gingham|plaid|tartan)\y', 14, null),
 ('boho',         'Boho / paisley',       '\y(boho|bohemian|paisley|mandala|ethnic|tribal)\y', 16, null),
 ('halloween',    'Halloween',            '\y(halloween|pumpkin|spooky|ghost|witch|skeleton)\y', 20, null),
 ('christmas',    'Christmas / holiday',  '\y(christmas|xmas|holiday|santa|snowflake|reindeer)\y', 20, null),
 ('pride',        'Pride / rainbow',      '\y(pride|rainbow|lgbt)\y', 22, null),
 ('clear',        'Clear / transparent',  '\y(clear|transparent|see ?through)\y', 30, 'A format, not a theme — pairs with any niche above'),
 ('magsafe',      'MagSafe',              '\ymagsafe\y', 32, 'Feature buyers search by name; competes on price and fit'),
 ('leather',      'Leather / PU',         '\y(leather|faux leather|pu leather)\y', 34, null),
 ('minimal',      'Minimal / plain',      '\y(minimalist|minimal|plain|simple|solid color)\y', 60, null)
on conflict (niche) do update
   set label = excluded.label, pattern = excluded.pattern,
       priority = excluded.priority, note = excluded.note;

-- ------------------------------------------------------------- case listings ---
-- Which listings are phone cases at all? Title is the honest signal: every case
-- seller stuffs the model name into it, while category_path is often just "Phone".
drop view if exists public.case_listings cascade;
create view public.case_listings
with (security_invoker = true) as
select l.etsy_id, l.title, l.shop_name, l.price_usd, l.favorites, l.listing_date,
       l.image_url, l.reviews, l.is_bestseller, l.is_star_seller, l.description,
       l.carts, l.bought_24h, l.first_keyword,
       lk.keyword, lk.best_rank
  from public.listings l
  join public.listing_keywords lk using (etsy_id)
 where l.is_ad is not true
   and l.title ~* '\y(phone case|phone cover|iphone|samsung|galaxy|pixel|magsafe|airpod|case for)\y'
   and l.title !~* '\y(shirt|tee|t-shirt|tshirt|sweatshirt|hoodie|mug|tumbler|sticker)\y';

-- ------------------------------------------------------------------- matview ---
drop materialized view if exists public.case_niche_mv cascade;
create materialized view public.case_niche_mv as
select c.etsy_id, c.keyword, dn.niche, c.best_rank, c.favorites, c.price_usd,
       c.shop_name, c.listing_date, c.image_url, c.title
  from public.case_listings c
  join lateral (
    select n.niche from public.case_niches n
     where c.title ~* n.pattern
     order by n.priority asc
     limit 1
  ) dn on true;

create unique index if not exists case_niche_mv_pk on public.case_niche_mv (keyword, etsy_id);
create index if not exists case_niche_mv_niche on public.case_niche_mv (niche);

create or replace function public.refresh_case_views() returns void
language plpgsql security definer set search_path = public as $$
begin
  refresh materialized view concurrently public.case_niche_mv;
end $$;

-- --------------------------------------------------------------------- stats ---
drop view if exists public.case_briefs, public.case_examples, public.case_production cascade;

create view public.case_stats
with (security_invoker = true) as
select '__cases'::text                                        as keyword,
       m.niche,
       count(*)                                               as listings,
       count(distinct m.shop_name)                            as shops,
       percentile_cont(0.5) within group (order by m.price_usd)::numeric(8,2) as median_price,
       -- A stored 0 is a parse artefact as often as a real count (see the crawler's
       -- favourites fallback), so medians are taken over known non-zero values and
       -- the unknown share is reported next to them instead of being averaged in.
       percentile_cont(0.5) within group (order by nullif(m.favorites, 0))::numeric as median_fav,
       percentile_cont(0.9) within group (order by nullif(m.favorites, 0))::numeric as p90_fav,
       percentile_cont(0.5) within group (
         order by m.favorites::numeric / greatest(1, current_date - m.listing_date)) as median_fav_per_day,
       round(100.0 * count(*) filter (where m.best_rank <= 30) / count(*), 0) as pct_in_top30,
       count(*) filter (where coalesce(m.favorites, 0) = 0)     as unknown_fav,
       count(*) filter (where m.listing_date is not null)       as dated,
       round(100.0 * count(*) filter (where m.listing_date >= current_date - 90)
             / greatest(1, count(*) filter (where m.listing_date is not null)), 0) as pct_new
  from public.case_niche_mv m
 group by m.niche;

create view public.case_briefs
with (security_invoker = true) as
with ranked as (
  select s.*, d.label, d.note,
         percent_rank() over (partition by s.keyword order by s.p90_fav nulls first) as demand_pct,
         percent_rank() over (partition by s.keyword order by s.listings)             as saturation_pct,
         percent_rank() over (partition by s.keyword order by s.pct_new nulls first)  as fresh_pct
    from public.case_stats s
    join public.case_niches d using (niche)
   where s.listings >= 8
)
select keyword, niche, label, note, listings, shops, median_price, median_fav, p90_fav,
       median_fav_per_day, pct_in_top30, pct_new, unknown_fav, dated,
       case when dated::numeric >= 0.3 * listings::numeric then fresh_pct else 0.5 end as fresh_used,
       round((100 * (0.55 * demand_pct
                     + 0.25 * case when dated::numeric >= 0.3 * listings::numeric
                                   then fresh_pct else 0.5 end
                     + 0.20 * (1 - saturation_pct))
              * least(1.0, listings::numeric / 30.0))::numeric, 0) as opportunity,
       round((100 * demand_pct)::numeric, 0)                  as demand_score,
       round((100 * (1 - saturation_pct))::numeric, 0)        as openness_score
  from ranked;

create view public.case_examples
with (security_invoker = true) as
select * from (
  select '__cases'::text as keyword, m.niche, m.etsy_id, m.title, m.image_url, m.price_usd,
         m.favorites, m.shop_name, m.best_rank, m.keyword as source_keyword,
         row_number() over (partition by m.niche
                            order by m.favorites desc nulls last) as rn
    from public.case_niche_mv m
) t
where rn <= 6;

-- What the cases are actually made of — the cost half of the decision.
create view public.case_production
with (security_invoker = true) as
with kinds as (
  select 'Silicone / TPU' as label, '\y(silicone|tpu|soft ?case|flexible)\y' as pat
  union all select 'Hard / polycarbonate', '\y(hard ?case|polycarbonate|\bpc\b|acrylic)\y'
  union all select 'Leather',              '\y(leather)\y'
  union all select 'MagSafe',              '\ymagsafe\y'
  union all select 'Shockproof / rugged',  '\y(shockproof|rugged|heavy ?duty|military ?grade)\y'
  union all select 'Wallet / flip',        '\y(wallet|flip|folio|card ?holder)\y'
)
select '__cases'::text as keyword, k.label, c.keyword as source_keyword,
       count(*)                                              as listings,
       round(100.0 * count(*) / (select count(*) from public.case_listings), 1) as pct_of_cases,
       percentile_cont(0.5) within group (order by nullif(c.favorites, 0))::numeric as median_fav
  from public.case_listings c
  join kinds k on c.description ~* k.pat
 group by c.keyword, k.label;

grant select on public.case_listings, public.case_niche_mv, public.case_stats,
                public.case_briefs, public.case_examples, public.case_production to authenticated;
revoke all on public.case_listings, public.case_stats, public.case_briefs,
              public.case_examples, public.case_production from anon;

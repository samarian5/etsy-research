-- 0017 — from "which keywords are good" to "what should I design next".
--
-- The board so far measures markets. This layer measures *designs*: it groups
-- the t-shirts already crawled into buyer-identity niches (dog mom, teacher,
-- retro bootleg, …), then scores each niche on demand against saturation — the
-- reverse-engineering step: find a crowded-looking market, find the pocket of it
-- where the winners are still beatable.
--
-- Everything here is derived from columns already stored: title (the only place
-- a niche is stated), favorites, price_usd, listing_date, best_rank,
-- shop_name and description (which is where the blank and the print method are
-- named — that is the production question, not the design question).
--
-- Favourites are the demand proxy: Etsy publishes no sold counts.

-- ── the niche dictionary ────────────────────────────────────────────────────
-- Patterns are regexes over the title, matched case-insensitively. They are
-- deliberately specific: "mom" alone would swallow "mommy", "grandma" and every
-- mother's-day shirt, which is exactly the over-grouping that makes niche lists
-- useless. Priority breaks ties when several match (lower wins).
create table if not exists public.design_niches (
    niche    text primary key,
    label    text not null,
    pattern  text not null,
    priority integer not null default 100,
    note     text
);

insert into public.design_niches (niche, label, pattern, priority, note) values
 ('dog',        'Dog / pet parent',      '\y(dog|dogs|puppy|pup|doggo|fur ?baby|pet photo|pet mom|pet dad)\y', 10, 'Includes custom pet-photo shirts'),
 ('cat',        'Cat',                   '\y(cat|cats|kitty|kitten|meow)\y', 10, null),
 ('pet_memorial','Pet memorial',         '(memorial|remembrance|in memory|rainbow bridge|loss of (my )?(dog|cat))', 5, 'High price, low volume'),
 ('mom',        'Mom',                   '\y(mom|mommy|mama|mum|mother)\y', 20, 'Excludes grandma/gigi via priority'),
 ('grandma',    'Grandma / Nana',        '\y(grandma|grandmother|gigi|nana|mimi|granny|abuela)\y', 12, 'Embroidered-heavy niche'),
 ('dad',        'Dad / Grandpa',         '\y(dad|daddy|father|grandpa|papa|grandad|abuelo)\y', 20, null),
 ('teacher',    'Teacher',               '\y(teacher|teaching|teach|educator|principal|school)\y', 10, null),
 ('nurse',      'Nurse / medical',       '\y(nurse|nursing|rn|med ?surg|doctor|paramedic|pharmacist)\y', 10, null),
 ('coffee',     'Coffee',                '\y(coffee|espresso|latte|caffeine|barista)\y', 15, null),
 ('wine',       'Wine / drinks',         '\y(wine|beer|whiskey|margarita|cocktail|vodka|drunk)\y', 15, null),
 ('fitness',    'Gym / fitness',         '\y(gym|lifting|workout|fitness|pilates|yoga|runner|running)\y', 15, null),
 ('outdoors',   'Camping / outdoors',    '\y(camp|camping|hike|hiking|fishing|hunt|hunting|national park|mountain|van life)\y', 15, null),
 ('western',    'Western / cowgirl',     '\y(western|cowgirl|cowboy|rodeo|boho|aztec|southwestern)\y', 12, null),
 ('retro',      'Retro / vintage',       '\y(retro|vintage|bootleg|90s|80s|70s|y2k|distressed)\y', 25, 'Style, not audience — often overlaps everything'),
 ('funny',      'Funny / sarcastic',     '\y(funny|sarcast|sassy|snarky|humor|humour|inappropriate|adult humor)\y', 30, null),
 ('faith',      'Faith / Christian',     '\y(jesus|faith|christian|god|blessed|bible|church|cross)\y', 12, null),
 ('halloween',  'Halloween',             '\y(halloween|spooky|ghost|witch|pumpkin|skeleton)\y', 12, null),
 ('christmas',  'Christmas / holiday',   '\y(christmas|xmas|santa|holiday|snowman|gingerbread)\y', 12, null),
 ('birthday',   'Birthday',              '\y(birthday|bday|b-day|born in|turning)\y', 15, null),
 ('wedding',    'Wedding / bachelorette','\y(wedding|bride|bridesmaid|bachelorette|engagement|hen party|bride squad)\y', 10, null),
 ('sports',     'Sports / team',         '\y(soccer|football|baseball|basketball|hockey|softball|volleyball|team mom|baseball mom)\y', 12, null),
 ('plants',     'Plants / garden',       '\y(plant|plants|plant mom|garden|gardening|succulent|fern)\y', 15, null),
 ('books',      'Books / reading',       '\y(book|books|reading|reader|bookish|bookworm|librarian)\y', 15, null),
 ('gaming',     'Gaming',                '\y(gamer|gaming|video game|console|dungeon|rpg|d20)\y', 15, null),
 ('mental',     'Mental health',         '\y(anxiety|mental health|self care|therapy|adhd|autism|neurodivergent)\y', 12, null),
 ('travel',     'Travel / places',       '\y(travel|road trip|airport|flight attendant|wanderlust|state|custom city)\y', 20, null),
 ('job',        'Occupation (generic)',  '\y(mechanic|electrician|welder|farmer|trucker|firefighter|police|engineer|realtor|barber|hair stylist|chef)\y', 20, null)
on conflict (niche) do update
  set label = excluded.label, pattern = excluded.pattern,
      priority = excluded.priority, note = excluded.note;

-- ── which listings are shirts at all ────────────────────────────────────────
-- A `tshirt` search returns mugs, totes and phone cases too. Counting those as
-- t-shirt designs would inflate every niche with items nobody can print on.
create or replace view public.shirt_listings as
select l.*, lk.keyword, lk.best_rank
  from public.listings l
  join public.listing_keywords lk using (etsy_id)
 where l.is_ad is not true
   and (l.title ~* '\y(shirt|shirts|tee|tees|t-shirt|t-shirt|tshirt|sweatshirt|hoodie|crewneck)\y'
        or coalesce(l.category_path, '') ~* '\y(shirt|tee)\y');

-- ── listing → niche (one niche per listing: the most specific match) ────────
create or replace view public.listing_niche as
select s.keyword, s.etsy_id, n.niche, s.best_rank, s.favorites, s.price_usd,
       s.shop_name, s.listing_date, s.image_url, s.title, s.description
  from public.shirt_listings s
  join lateral (
        select dn.niche
          from public.design_niches dn
         where s.title ~* dn.pattern
         order by dn.priority
         limit 1
  ) n on true;

-- ── niche economics ─────────────────────────────────────────────────────────
-- demand = what the winners in this niche actually have (p90 favourites)
-- saturation = how many listings and shops are already there
-- freshness = share of the niche published in the last 90 days
create or replace view public.niche_stats as
with base as (
    select keyword, niche, etsy_id, favorites, price_usd, shop_name, listing_date, best_rank
      from public.listing_niche
)
select keyword,
       niche,
       count(*)                                                            as listings,
       count(distinct shop_name)                                           as shops,
       round(percentile_cont(0.5) within group (order by price_usd::double precision)::numeric, 2) as median_price,
       round(percentile_cont(0.5) within group (order by favorites::double precision)::numeric, 0) as median_fav,
       round(percentile_cont(0.9) within group (order by favorites::double precision)::numeric, 0) as p90_fav,
       round(percentile_cont(0.5) within group (order by favorites::double precision / greatest(1, current_date - listing_date))::numeric, 2) as median_fav_per_day,
       round(100.0 * count(*) filter (where best_rank <= 30) / count(*), 1) as pct_in_top30,
       round(100.0 * count(*) filter (where listing_date >= current_date - 90) / nullif(count(*) filter (where listing_date is not null), 0), 1) as pct_new,
       count(*) filter (where favorites is null)                          as unknown_fav,
       count(*) filter (where listing_date is not null)                    as dated
  from base
 group by keyword, niche;

-- ── the decision table ──────────────────────────────────────────────────────
-- Percentile ranks are computed *within* a keyword, because the question is
-- always "which niche inside this market", never "is tshirt bigger than mugs".
drop view if exists public.design_briefs;
create view public.design_briefs as
with ranked as (
    select n.*,
           d.label,
           d.note,
           percent_rank() over (partition by keyword order by p90_fav nulls first)      as demand_pct,
           percent_rank() over (partition by keyword order by listings)                 as saturation_pct,
           percent_rank() over (partition by keyword order by pct_new nulls first)      as fresh_pct,
           percent_rank() over (partition by keyword order by median_price nulls first) as price_pct
      from public.niche_stats n
      join public.design_niches d using (niche)
     where n.listings >= 8                        -- below that a median is noise
)
select keyword, niche, label, note, listings, shops, median_price, median_fav, p90_fav,
       median_fav_per_day, pct_in_top30, pct_new, unknown_fav, dated,
       -- Freshness only counts when enough of the niche actually has a publish
       -- date; the backfill is still running, so a niche whose dated rows happen
       -- to be all-new would otherwise score as a gold rush. Below 30% dated it
       -- is treated as unknown (neutral 0.5) instead of 100%.
       case when dated >= 0.3 * listings then fresh_pct else 0.5 end as fresh_used,
       -- Shrink towards zero below 30 listings: a 10-listing niche with one viral
       -- shirt must not outrank a 70-listing niche with a real median.
       round((100 * (0.55 * demand_pct
                     + 0.25 * (case when dated >= 0.3 * listings then fresh_pct else 0.5 end)
                     + 0.20 * (1 - saturation_pct))
              * least(1.0, listings / 30.0))::numeric, 0) as opportunity,
       round((100 * demand_pct)::numeric, 0)   as demand_score,
       round((100 * (1 - saturation_pct))::numeric, 0) as openness_score
  from ranked;

-- ── the designs themselves ──────────────────────────────────────────────────
-- Six winners per niche, with the thumbnail, so a niche can be judged by eye in
-- one screen instead of by a table of medians.
create or replace view public.niche_examples as
select s.keyword, s.niche,
       e.etsy_id, e.title, e.image_url, e.price_usd, e.favorites, e.shop_name, e.best_rank
  from (select distinct keyword, niche from public.listing_niche) s
  join lateral (
        select ln.etsy_id, ln.title, ln.image_url, ln.price_usd, ln.favorites,
               ln.shop_name, ln.best_rank
          from public.listing_niche ln
         where ln.keyword = s.keyword and ln.niche = s.niche
           and ln.favorites is not null
           and ln.image_url is not null
         order by ln.favorites desc
         limit 6
  ) e on true;

-- ── the production question ────────────────────────────────────────────────
-- The blank and the print method are named in the description of nearly every
-- winning shirt, and they decide cost, margin and feel. This is the one place
-- the description is worth reading at scale.
create or replace view public.production_stats as
with s as (
    select keyword, etsy_id, favorites, description
      from public.shirt_listings
     where description is not null and length(description) > 80
), m(label, pattern) as (
    values ('Comfort Colors', '(comfort colors|comfort colour)'),
           ('Gildan',         '\ygildan\y'),
           ('Bella+Canvas',   '(bella ?\+? ?canvas)'),
           ('Next Level',     'next level'),
           ('DTF',            '\ydtf\y'),
           ('Screen print',   'screen ?print'),
           ('DTG',            '\ydtg\y'),
           ('Embroidery',     'embroider'),
           ('Sublimation',    'sublimat'),
           ('Vintage / acid wash', '(acid wash|garment.?dyed|vintage wash)')
)
select s.keyword, m.label, count(*) as listings,
       round(100.0 * count(*) / nullif((select count(*) from s s2 where s2.keyword = s.keyword), 0), 1) as pct_of_shirts,
       round(percentile_cont(0.5) within group (order by s.favorites::double precision)::numeric, 0) as median_fav
  from s
  join m on s.description ~* m.pattern
 group by s.keyword, m.label;

-- ── access: same rules as every other view ─────────────────────────────────
alter view public.shirt_listings   set (security_invoker = true);
alter view public.listing_niche    set (security_invoker = true);
alter view public.niche_stats      set (security_invoker = true);
alter view public.design_briefs    set (security_invoker = true);
alter view public.niche_examples   set (security_invoker = true);
alter view public.production_stats set (security_invoker = true);

revoke all on public.shirt_listings, public.listing_niche, public.niche_stats,
              public.design_briefs, public.niche_examples, public.production_stats from anon;
grant select on public.shirt_listings, public.listing_niche, public.niche_stats,
                public.design_briefs, public.niche_examples, public.production_stats to authenticated;

notify pgrst, 'reload schema';

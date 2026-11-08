-- 0020_ingest_gallery.sql — the RPC is the only write path, and it silently dropped
-- the new columns: the crawler was sending `gallery`, `carts` and `bought_24h` on
-- every row while ingest_batch's INSERT listed 27 columns and none of them. That is
-- why 141 freshly crawled listings arrived with an image_count but no gallery.
-- Recreated here with the three columns added, and COALESCE on conflict so a page
-- that renders without a gallery can never erase one already stored.

CREATE OR REPLACE FUNCTION public.ingest_batch(p_run_id text, p_rows jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
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
            shop_reviews, shop_rating, discount_pct, original_price_usd, is_ad,
            gallery, carts, bought_24h
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
            (nullif(r->>'is_ad', ''))::boolean,
            -- 0020: the design half. JSON-LD galleries arrive as a JSON array of
            -- CDN urls; carts / bought-in-24h are Etsy's urgency counters.
            case when jsonb_typeof(r->'gallery') = 'array' then r->'gallery' end,
            nullif(r->>'carts', '')::int,
            nullif(r->>'bought_24h', '')::int
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
            gallery        = coalesce(excluded.gallery, l.gallery),
            carts          = coalesce(excluded.carts, l.carts),
            bought_24h     = coalesce(excluded.bought_24h, l.bought_24h),
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
$function$

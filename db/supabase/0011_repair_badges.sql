-- 0011 — discard the badges the old whole-page regexes invented
--
-- Before 0010 every listing page was scanned with re.search(r"\bstar seller\b")
-- and re.search(r"\bbestseller\b") over the whole document. Etsy prints the Star
-- Seller explainer on pages whose seller is not a Star Seller, and labels related
-- cards "Bestseller", so the flags came out at 44% and 27% of the catalogue —
-- rates that cannot exist. Same story for "free shipping", which matched the
-- site-wide "Free shipping on orders over $35" promo.
--
-- Rather than guess, the bad values are nulled. Null means "not measured", which
-- is true; the corrected parser refills each listing the next time it is crawled
-- (ingest_batch coalesces, so a null never overwrites a good value).
--
-- This is a one-time repair: it is NOT part of 0010, so re-applying 0010 later
-- cannot wipe correctly-measured flags.

update public.listings set is_star_seller = null where is_star_seller is not null;
update public.listings set is_bestseller  = null where is_bestseller  is not null;
update public.listings set free_shipping  = null where free_shipping  is not null;

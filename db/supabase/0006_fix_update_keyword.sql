-- 0006_fix_update_keyword.sql
--
-- update_keyword() used COALESCE(GREATEST(1, LEAST(100, p_depth)), depth) to mean
-- "leave depth alone when the caller passes NULL". That does not work: LEAST and
-- GREATEST ignore NULL arguments in Postgres rather than returning NULL, so
-- LEAST(100, NULL) is 100 and the guard never fires. Any call that only wanted to
-- change the interval silently pushed depth to its maximum and pages_per_run to 5.
--
-- Each nullable field now branches explicitly with CASE.

create or replace function public.update_keyword(
    p_token          text,
    p_keyword        text,
    p_interval_hours int  default null,
    p_depth          int  default null,
    p_pages_per_run  int  default null
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
    v_kw  text := lower(btrim(regexp_replace(coalesce(p_keyword, ''), '\s+', ' ', 'g')));
    v_int int  := case when p_interval_hours is null then null
                       else greatest(1, least(24, p_interval_hours)) end;
    v_row public.keywords;
begin
    if not public.check_token(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;

    update public.keywords
       set interval_hours = coalesce(v_int, interval_hours),
           depth          = case when p_depth is null then depth
                                 else greatest(1, least(100, p_depth)) end,
           pages_per_run  = case when p_pages_per_run is null then pages_per_run
                                 else greatest(1, least(5, p_pages_per_run)) end,
           -- A new cadence starts now. Pushing it forward is the point: going from
           -- 2h to 24h must not leave the keyword claimable on the next tick.
           due_at         = case when v_int is null then due_at
                                 else now() + make_interval(hours => v_int) end
     where keyword = v_kw
    returning * into v_row;

    if not found then
        raise exception 'no such keyword: %', v_kw;
    end if;

    return jsonb_build_object('ok', true, 'keyword', v_row.keyword,
                              'interval_hours', v_row.interval_hours,
                              'depth', v_row.depth,
                              'pages_per_run', v_row.pages_per_run,
                              'due_at', v_row.due_at);
end;
$$;

revoke all on function public.update_keyword(text, text, int, int, int) from anon, authenticated;
grant execute on function public.update_keyword(text, text, int, int, int) to authenticated, anon;

-- 0023_case_niches_widen.sql — cover the case titles that matched no style.
--
-- A third of case listings fell outside the first 24 niches, and the uncovered sample
-- was not noise: quotes/sayings, faith, gothic, fruit, polka dots, fine-art prints,
-- autumn and licensed fan art are all real, recurring case genres. Priorities sit
-- above the generic 'minimal' bucket so a specific genre wins the tie.

insert into public.case_niches (niche, label, pattern, priority, note) values
 ('licensed',  'Licensed / fan art',  '\y(pokemon|pikachu|disney|winnie the pooh|sanrio|marvel|stitch|bluey|barbie|harry potter|star wars)\y', 4,  'Trademark risk — read the composition, never the character'),
 ('quote',     'Quote / funny text',  '\y(sarcasm|sarcastic|funny|humou?r|quote|saying|slogan|snarky|pun)\y', 9,  'The shirt-market skill that transfers most directly to cases'),
 ('faith',     'Faith / Christian',   '\y(christian|bible|jesus|faith|blessed|prayer|scripture|god is)\y', 11, null),
 ('gothic',    'Gothic / occult',     '\y(gothic|skull|skeletons?|occult|baphomet|baroque|dark academia|witchy|tarot)\y', 13, null),
 ('fruit',     'Fruit / food',        '\y(cherry|cherries|strawberr|lemon|orange slice|watermelon|peach|avocado|fruit)\y', 15, null),
 ('dots',      'Polka dot / spotted', '\y(polka ?dot|dots\b|spotted)\y', 16, null),
 ('art_print', 'Fine-art print',      '\y(fine art|art print|painting|impressionis|van gogh|monet|matisse|renaissance|oil painting)\y', 17, null),
 ('autumn',    'Autumn / seasonal',   '\y(autumn|maple|harvest|thanksgiving|fall leaves|pumpkin spice)\y', 18, null)
on conflict (niche) do update
   set label = excluded.label, pattern = excluded.pattern,
       priority = excluded.priority, note = excluded.note;

select public.refresh_case_views();

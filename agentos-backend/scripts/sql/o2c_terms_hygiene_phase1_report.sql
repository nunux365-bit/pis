-- Phase 1 read-only: per-site terms hygiene preview (no writes).
-- Rules: winner = latest MIS max(billing_period_end); expire other MIS-used on site;
--        pending CTVs never on any o2c_mis_run; winner gets min(from)/max(to).

WITH mis_ctv AS (
    SELECT DISTINCT contract_terms_version_id AS ctv_id
    FROM o2c_mis_run
),
site_ctv AS (
    SELECT DISTINCT crl.service_site_id AS site_id, crl.contract_terms_version_id AS ctv_id
    FROM contract_rate_line crl
    WHERE crl.service_site_id IS NOT NULL
      AND crl.is_active = true
),
site_scope AS (
    SELECT DISTINCT site_id FROM site_ctv
    UNION
    SELECT DISTINCT service_site_id FROM o2c_mis_run
),
latest_mis AS (
    SELECT DISTINCT ON (mr.service_site_id)
        mr.service_site_id,
        mr.contract_terms_version_id AS winner_ctv_id,
        mr.billing_period_end
    FROM o2c_mis_run mr
    ORDER BY mr.service_site_id, mr.billing_period_end DESC, mr.updated_at DESC
),
rule_b_winner AS (
    SELECT DISTINCT ON (sc.site_id)
        sc.site_id,
        ctv.id AS winner_ctv_id
    FROM site_scope sc
    JOIN site_ctv st ON st.site_id = sc.site_id
    JOIN contract_terms_version ctv ON ctv.id = st.ctv_id
    LEFT JOIN latest_mis lm ON lm.service_site_id = sc.site_id
    WHERE lm.service_site_id IS NULL
    ORDER BY sc.site_id, ctv.created_at DESC
),
site_winner AS (
    SELECT site_id, winner_ctv_id, 'latest_mis'::text AS rule
    FROM latest_mis
    UNION ALL
    SELECT site_id, winner_ctv_id, 'no_mis_newest'::text
    FROM rule_b_winner
),
site_candidates AS (
    SELECT sw.site_id, sw.winner_ctv_id, sw.rule, st.ctv_id AS candidate_ctv_id
    FROM site_winner sw
    JOIN site_ctv st ON st.site_id = sw.site_id
),
date_bounds AS (
    SELECT
        sc.site_id,
        min(ctv.effective_from) AS new_from,
        max(COALESCE(ctv.effective_to, '9999-12-31'::date)) AS new_to_cap
    FROM site_candidates sc
    JOIN contract_terms_version ctv ON ctv.id = sc.candidate_ctv_id
    GROUP BY sc.site_id
),
planned AS (
    SELECT
        ss.id AS service_site_id,
        COALESCE(ss.site_key, ss.display_name, ss.id::text) AS site_key,
        bc.name AS billing_client_name,
        sw.winner_ctv_id,
        sw.rule AS winner_rule,
        wctv.status AS winner_status_now,
        wctv.effective_from AS winner_from_now,
        wctv.effective_to AS winner_to_now,
        db.new_from AS winner_from_planned,
        CASE WHEN db.new_to_cap = '9999-12-31'::date THEN NULL ELSE db.new_to_cap END AS winner_to_planned,
        st.ctv_id AS candidate_ctv_id,
        ctv.status AS candidate_status_now,
        CASE
            WHEN NOT EXISTS (SELECT 1 FROM mis_ctv m WHERE m.ctv_id = st.ctv_id) THEN 'pending'
            WHEN EXISTS (
                SELECT 1 FROM o2c_mis_run mr
                WHERE mr.service_site_id = ss.id
                  AND mr.contract_terms_version_id = st.ctv_id
            ) AND st.ctv_id <> sw.winner_ctv_id THEN 'expire'
            WHEN st.ctv_id = sw.winner_ctv_id THEN 'approve'
            ELSE 'pending'
        END AS planned_action
    FROM site_scope sco
    JOIN service_site ss ON ss.id = sco.site_id
    JOIN billing_client bc ON bc.id = ss.billing_client_id
    JOIN site_winner sw ON sw.site_id = ss.id
    JOIN site_ctv st ON st.site_id = ss.id
    JOIN contract_terms_version wctv ON wctv.id = sw.winner_ctv_id
    JOIN contract_terms_version ctv ON ctv.id = st.ctv_id
    LEFT JOIN date_bounds db ON db.site_id = ss.id
)
SELECT *
FROM planned
ORDER BY billing_client_name, site_key, planned_action, candidate_ctv_id;

-- Summary counts
-- SELECT planned_action, count(*) FROM planned GROUP BY 1 ORDER BY 1;

-- Multi-site CTV warning: one terms version used on MIS for multiple service sites
-- SELECT ctv_id, count(DISTINCT service_site_id) AS mis_sites
-- FROM o2c_mis_run GROUP BY 1 HAVING count(DISTINCT service_site_id) > 1;

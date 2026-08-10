SELECT AVG(CASE WHEN value >= 5 THEN 1.0 ELSE 0.0 END) FROM measurements;

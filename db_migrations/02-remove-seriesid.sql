ALTER TABLE patch RENAME TO patch_old;
CREATE TABLE patch(
        id INTEGER PRIMARY KEY,
        name TEXT,
        url TEXT,
        date TEXT,
        patchsource_id INTEGER,
        FOREIGN KEY(patchsource_id) REFERENCES patchsource(id)
);

INSERT INTO patch
        SELECT id, name, url, date, patchsource_id FROM patch_old;

DROP TABLE patch_old;

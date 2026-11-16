package com.shop.report;

import java.util.ArrayList;
import java.util.List;

public class ReportService {
    public List<Row> exportAll(Repo repo) {
        List<Row> rows = new ArrayList<>();
        rows.addAll(repo.findAll());
        return rows;
    }
}

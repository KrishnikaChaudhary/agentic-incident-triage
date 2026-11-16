package com.shop.config;

import com.zaxxer.hikari.HikariConfig;

public class DataSourceConfig {
    public HikariConfig hikari() {
        HikariConfig cfg = new HikariConfig();
        cfg.setMaximumPoolSize(5);
        return cfg;
    }
}

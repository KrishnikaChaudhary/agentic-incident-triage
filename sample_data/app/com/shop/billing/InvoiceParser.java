package com.shop.billing;

import com.fasterxml.jackson.databind.ObjectMapper;

public class InvoiceParser {
    private final ObjectMapper mapper = new ObjectMapper();

    public Invoice parse(String json) throws Exception {
        return mapper.readValue(json, Invoice.class);
    }
}

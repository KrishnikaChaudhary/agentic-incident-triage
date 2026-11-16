package com.shop.payment;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;

public class PaymentClient {
    private final HttpClient http = HttpClient.newHttpClient();

    public Receipt charge(Order order) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(order.endpoint()).build();
        return http.send(req, Receipt.handler()).body();
    }
}

package com.shop.order;

import com.shop.customer.Customer;
import com.shop.customer.CustomerRepository;

public class OrderService {
    private final CustomerRepository customers;

    public OrderService(CustomerRepository customers) {
        this.customers = customers;
    }

    public String shippingLabel(long customerId) {
        Customer c = customers.findById(customerId);
        return c.getAddress().format();
    }
}

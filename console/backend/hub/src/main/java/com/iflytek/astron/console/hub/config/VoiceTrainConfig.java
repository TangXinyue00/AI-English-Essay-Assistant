package com.iflytek.astron.console.hub.config;

import cn.xfyun.api.VoiceTrainClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class VoiceTrainConfig {

    @Value("${spark.app-id}")
    private String appId;

    @Value("${spark.api-key}")
    private String apiKey;

    @Bean
    public VoiceTrainClient voiceTrainClient() {
        return new VoiceTrainClient.Builder(appId, apiKey).build();
    }
}

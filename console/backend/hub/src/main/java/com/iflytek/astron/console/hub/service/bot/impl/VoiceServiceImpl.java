package com.iflytek.astron.console.hub.service.bot.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.iflytek.astron.console.commons.util.I18nUtil;
import com.iflytek.astron.console.hub.entity.PronunciationPersonConfig;
import com.iflytek.astron.console.hub.enums.TtsTypeEnum;
import com.iflytek.astron.console.hub.mapper.PronunciationPersonConfigMapper;
import com.iflytek.astron.console.hub.service.bot.VoiceService;
import com.iflytek.astron.console.toolkit.tool.http.HttpAuthTool;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * @author bowang
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class VoiceServiceImpl implements VoiceService {

    private static final String TTS_API_URL = "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6";

    @Value("${spark.app-id}")
    private String appId;

    @Value("${spark.api-key}")
    private String apiKey;

    @Value("${spark.api-secret}")
    private String apiSecret;

    private final PronunciationPersonConfigMapper pronunciationPersonConfigMapper;

    @Override
    public Map<String, String> getTtsSign() {
        Map<String, String> resultMap = new HashMap<>();
        String url = HttpAuthTool.assembleRequestUrl(TTS_API_URL, apiKey, apiSecret);
        resultMap.put("appId", appId);
        resultMap.put("url", url);
        resultMap.put("type", TtsTypeEnum.ORIGINAL.name());
        return resultMap;
    }

    @Override
    @Cacheable(value = "pronunciationPersonCache", key = "#root.methodName", cacheManager = "cacheManager5min")
    public List<PronunciationPersonConfig> getPronunciationPerson() {
        LambdaQueryWrapper<PronunciationPersonConfig> queryWrapper = new LambdaQueryWrapper<>();
        queryWrapper.eq(PronunciationPersonConfig::getSpeakerType, PronunciationPersonConfig.SpeakerTypeEnum.NORMAL);
        queryWrapper.eq(PronunciationPersonConfig::getDeleted, 0);
        queryWrapper.orderByAsc(PronunciationPersonConfig::getSort);
        List<PronunciationPersonConfig> configList = pronunciationPersonConfigMapper.selectList(queryWrapper);
        // Convert name field from key to internationalized value
        for (PronunciationPersonConfig config : configList) {
            if (config.getName() != null) {
                config.setName(I18nUtil.getMessage(config.getName()));
            }
        }
        return configList;
    }
}

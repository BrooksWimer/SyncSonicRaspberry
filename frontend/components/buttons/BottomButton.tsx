import React, { useRef } from 'react';
import { Button } from 'tamagui';
import { ActivityIndicator, Text } from 'react-native';
import { useAppColors } from '../../styles/useAppColors';

type BottomButtonProps = {
  text?: string;
  onPress: () => void;
  disabled?: boolean;
  isLoading?: boolean;
  iosBuffer?: number;
  style?: any;
  children?: React.ReactNode;
  fontFamily?: string;
};

export const BottomButton = ({
  text,
  onPress,
  disabled = false,
  isLoading = false,
  iosBuffer = 0,
  style = {},
  children,
  fontFamily = 'Inter',
}: BottomButtonProps) => {
  const { pc } = useAppColors();
  const debounceRef = useRef(false);

  const handlePress = () => {
    if (debounceRef.current || disabled || isLoading) return;

    debounceRef.current = true;
    onPress();

    // Reset after 500ms
    setTimeout(() => {
      debounceRef.current = false;
    }, 1000);
  };

  const buttonText = isLoading ? 'Loading...' : text;

  return (
    <Button
      onPress={handlePress}
      disabled={disabled || isLoading}
      style={[
        {
          position: 'absolute',
          bottom: 20,
          backgroundColor: pc,
          width: '90%',
          height: 50,
          borderRadius: 15,
          alignSelf: 'center',
          justifyContent: 'center',
          alignItems: 'center',
          marginBottom: 20,
          marginTop: 50 + iosBuffer,
          opacity: disabled || isLoading ? 0.5 : 1,
        },
        style,
      ]}
      pressStyle={{ opacity: disabled || isLoading ? 0.5 : 0.8 }}
    >
      {children ? (
        children
      ) : isLoading ? (
        <ActivityIndicator color="#fff" />
      ) : (
        <Text style={{ color: 'white', fontSize: 18, fontFamily }}>
          {buttonText}
        </Text>
      )}
    </Button>
  );
};
